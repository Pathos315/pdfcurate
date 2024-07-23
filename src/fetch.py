r"""Contains methods that take various files and directories, which each
returning various dataframes for each"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Generator, TypeVar

import pandas as pd
import uuid
from tqdm import tqdm

from src.change_dir import change_dir
from src.config import KEY_TYPE_PAIRINGS, FilePath, config
from src.docscraper import DocScraper, DocumentResult
from src.downloaders import Downloader, DownloadReceipt
from src.log import logger
from src.webscrapers import WebScraper, WebScrapeResult

SerializationStrategyFunction = Callable[[Path], list[Any]]
StagingStrategyFunction = Callable[[pd.DataFrame], Iterable[Any]]
ScrapeResult = DocumentResult | WebScrapeResult | DownloadReceipt
Scraper = DocScraper | WebScraper | Downloader


T = TypeVar("T", bound=ScrapeResult)


@dataclass
class Fetcher(ABC):
    """
    Fetcher is the overarching abstract class for fetching data
    from a given query.
    """

    scraper: Scraper

    @abstractmethod
    def __call__(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        """The __call__ method in ScrapeFetcher and StagingFetcher
        take a target FilePath or dataframe respectively.
        They are converted them into lists of entries.
        Next those lists are scraped in `fetch`, where they are
        passed into a Generator, which ultimately produces
        a list that's made into a pd.DataFrame.

        Returns
        -------
        pd.DataFrame
            A dataframe containing biliographic data.
        """

    def fetch(
        self, search_terms: list[str], tqdm_unit: str = "abstracts"
    ) -> Generator[ScrapeResult, None, None]:
        """
        fetch runs a scrape using the given search terms and yields results.

        Parameters
        ----------
        search_terms : list[str]
            A serialized list of terms to be scraped.

        Yields
        -------
        ScrapeResult
            A result containing bibliographic data.
        """
        for term in tqdm(
            search_terms, desc="[sciscraper]: ", unit=f"{tqdm_unit}"
        ):
            results = self.scraper.obtain(term)
            if isinstance(results, Generator):
                yield from results
            elif results is not None:
                yield results


@dataclass
class ScrapeFetcher(Fetcher):
    """
    ScrapeFetcher takes a string `target`
    serialized into a list of strings with serializer
    It then puts it into fetch, where it returns a dataframe.
    """

    serializer: SerializationStrategyFunction
    title_serializer: SerializationStrategyFunction | None = None

    def __call__(self, target: Path) -> pd.DataFrame:
        search_terms: list[str] = self.serializer(target)
        results = list(self.fetch(search_terms))
        df = pd.DataFrame(results)
        if self.title_serializer:
            df["title"] = self.title_serializer(target)
        return df


@dataclass
class StagingFetcher(Fetcher):
    """
    StagingFetcher takes a dataframe, `prior_dataframe` with one of its columns
    isolated and staged, via `stager`, into a list of `staged_terms`.
    It then puts it into fetch, where it returns the final dataframe.
    """

    stager: StagingStrategyFunction

    def __call__(self, prior_dataframe: pd.DataFrame) -> pd.DataFrame:
        staged_terms: Iterable[Any] = self.stager(prior_dataframe)
        if isinstance(staged_terms, list):
            return self.fetch_from_staged_series(prior_dataframe, staged_terms)
        elif isinstance(staged_terms, tuple):
            return self.fetch_with_staged_reference(staged_terms)
        else:
            raise ValueError("Staged terms must be lists or tuples.")

    def fetch_from_staged_series(
        self, prior_dataframe: pd.DataFrame, staged_terms: list[Any]
    ) -> pd.DataFrame:
        """If the terms are staged as a list, then the dataframe is extended
        along the provided query, and then it is appended to the existing dataframe.
        """
        results = list(self.fetch(staged_terms))
        dataframe_ext: pd.DataFrame = pd.DataFrame(results)
        dataframe: pd.DataFrame = prior_dataframe.join(dataframe_ext)
        return dataframe

    def fetch_with_staged_reference(
        self,
        staged_terms: tuple[
            list[Any],
            list[Any],
        ],
    ) -> pd.DataFrame:
        """If the terms are staged as a tuple of two lists,
        then the first part of the tuple gets extended
        along the provided query. Then the second part of the tuple gets
        exploded out and appended. The default version of this
        provide the source titles, from which the ensuing citations
        were originally found. The prior dataframe is not kept."""
        citations, src_titles = staged_terms
        results = list(self.fetch(citations))
        print(results)
        ref_dataframe = pd.DataFrame(results)
        dataframe = ref_dataframe.join(
            pd.Series(
                src_titles,
                dtype="string",
                name="source_titles",
            )
        )
        return dataframe


@dataclass
class SciScraper:
    """
    Sciscraper is the base class for all
    operations within the sciscraper module.
    """

    scraper: ScrapeFetcher
    stager: StagingFetcher | None
    logger = logger
    downcast: bool = True
    debug: bool = True
    export: bool = True

    def __call__(
        self,
        target: Path,
    ) -> None:
        self.set_logging()
        logger.info(
            "Debug logging status: '%s'\n"
            "Commencing sciscrape on file: '%s'...\n",
            self.debug,
            target,
        )
        dataframe: pd.DataFrame = self.scraper(target)
        dataframe = self.stager(dataframe) if self.stager else dataframe
        dataframe = self.remove_empty_columns(dataframe)
        dataframe = (
            self.dataframe_casting(dataframe) if self.downcast else dataframe
        )
        self.export_sciscrape_results(dataframe) if self.export else None

    def set_logging(self) -> None:
        """Sets the logging level to debug if specified,
        otherwise, it defaults to info logging."""
        self.logger.setLevel(10) if self.debug else self.logger.setLevel(20)

    def remove_empty_columns(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        """Removes all empty columns in the dataframe before exporting to .csv."""
        return dataframe.replace("", float("NaN")).dropna(how="all", axis=1)

    @staticmethod
    def dataframe_casting(dataframe: pd.DataFrame) -> pd.DataFrame:
        """
        Formats and optimizes the final dataframe through converting datatypes
        and filling missing fields, where possible.

        Returns
        -------
        pd.DataFrame
            A cleaned and optimized dataframe.
        """

        # Convert "pub_date" column to datetime data type
        if "pub_date" in dataframe:
            dataframe["pub_date"] = SciScraper.downcast_available_datetimes(
                dataframe
            )

        # Convert columns in KEY_TYPE_PAIRINGS dictionary to specified data types
        for scikey, value in KEY_TYPE_PAIRINGS.items():
            if scikey in dataframe:
                dataframe[scikey] = dataframe[scikey].astype(value)
        return dataframe

    @staticmethod
    def downcast_available_datetimes(
        dataframe: pd.DataFrame | pd.Series[Any],
    ) -> pd.Timestamp:
        """Converts all paper publication dates to the datetime format."""
        return pd.to_datetime(dataframe["pub_date"], errors="coerce")  # type: ignore[return-value]

    @staticmethod
    def export_sciscrape_results(
        dataframe: pd.DataFrame,
        export_dir: FilePath = Path(config.export_dir),
    ) -> None:
        """Export data to the specified export directory."""
        SciScraper.dataframe_logging(dataframe)
        export_name = SciScraper.create_export_name()
        with change_dir(export_dir):
            logger.info(
                "A spreadsheet was exported as %s in %s.",
                export_name,
                export_dir,
            )
            dataframe.to_csv(export_name, index=False)

    @staticmethod
    def dataframe_logging(dataframe: pd.DataFrame) -> None:
        """Returns the first ten rows of the dataframe into the logger."""
        dataframe.info(verbose=True)
        logger.info("\n\n%s", dataframe.head(10))

    @staticmethod
    def create_export_name() -> FilePath:
        """Returns a `export_name` for the spreadsheet with
        both today's date and a randomly generated `print_id`
        number."""
        unique_id: str = str(uuid.uuid4())[:4]
        return Path(f"{config.today}_sciscraper_{unique_id}.csv")
