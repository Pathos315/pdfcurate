from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from enum import Enum
from time import sleep

from json import loads
from selectolax.parser import HTMLParser
from requests import Response, Session
from sciscraper.scrape.log import logger
from sciscraper.scrape.config import config


client = Session()

DIMENSIONS_AI_KEYS: dict[str, str] = {
    "title": "title",
    "pub_date": "pub_date",
    "doi": "doi",
    "internal_id": "id",
    "journal_title": "journal_title",
    "times_cited": "times_cited",
    "author_list": "author_list",
    "citations": "cited_dimensions_ids",
    "keywords": "mesh_terms",
}


@dataclass(slots=True, frozen=True, order=True)
class WebScrapeResult:
    """Represents a result from a scrape to be passed back to the dataframe."""

    title: str
    pub_date: str
    doi: str
    internal_id: Optional[str]
    journal_title: Optional[str]
    times_cited: Optional[int]
    author_list: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    keywords: Optional[list[str]] = field(default_factory=list)
    figures: Optional[list[str]] = field(default_factory=list)
    biblio: Optional[str] = None
    abstract: Optional[str] = None

    @classmethod
    def from_dict(cls, dict_input: dict):
        return WebScrapeResult(**dict_input)

    def to_dict(self):
        return asdict(self)


@dataclass(slots=True)
class WebScraper(ABC):
    """Abstract representation of a webscraper dataclass."""

    url: str
    sleep_val: float = config.sleep_interval

    @abstractmethod
    def obtain(self, search_text: str) -> Optional[WebScrapeResult]:
        """
        obtain takes the requested identifier string, `search_text`
        normally a digital object identifier (DOI), or similar,
        and requests it from the provided citational dataset,
        which returns bibliographic data on the paper(s) requested.

        Parameters
        ----------
        search_text : str
            the pubid or digital object identifier
            (i.e. DOI) of the paper in question

        Returns
        -------
        WebScrapeResult
            a dataclass containing the requested information,
            which gets sent back to a dataframe.
        """


@dataclass(slots=True)
class DimensionsScraper(WebScraper):
    """
    Representation of a webscraper that makes requests to dimensions.ai.
    This is sciscraper's preferred webscraper.
    """

    query_subset_citations: bool = False

    def obtain(self, search_text: str) -> Optional[WebScrapeResult]:
        querystring: dict[str, str] = self.create_querystring(search_text)
        response = client.get(self.url, params=querystring)
        sleep(self.sleep_val)

        logger.debug(
            "search_text=%s, scraper=%s, status_code=%s",
            search_text,
            repr(self),
            response.status_code,
        )
        if response.status_code != 200:
            return None
        data = self.clean_response(response)
        return WebScrapeResult(**data)

    def clean_response(self, response: Response) -> dict[str, Any]:
        api_keys: dict[str, str] = DIMENSIONS_AI_KEYS

        getters: dict[str, tuple[str, WebScraper]] = {
            "biblio": (
                "doi",
                CitationScraper(config.citation_crosscite_url, sleep_val=0.1),
            ),
            "abstract": (
                "internal_id",
                SummaryScraper(config.abstract_getting_url, sleep_val=0.1),
            ),
            "figures": (
                "title",
                SemanticFigureScraper(config.semantic_scholar_url_stub, sleep_val=0.1),
            ),
        }

        docs = loads(response.text)["docs"]
        item = docs[0]
        data: dict[str, Any] = {
            key: item.get(value) for (key, value) in api_keys.items()
        }
        for key, getter in getters.items():
            data[key] = self.get_extra_variables(data, getter[0], getter[1])
        return data

    def get_extra_variables(
        self, data: dict[str, Any], query: str, getter: WebScraper
    ) -> Optional[WebScrapeResult]:
        """get_extra_variables queries
        subsidiary scrapers to get
        additional data

        Parameters
        ----------
        data : dict
            the dict from the initial scrape
        getter : WebScraper
            the subsidiary scraper that
            will obtain additional information
        query : str
            the existing `data` to be queried.
        """
        try:
            return getter.obtain(data[query])
        except KeyError as e:
            logger.error(
                "func_repr=%s, query=%s, error=%s, action_undertaken=%s",
                repr(getter),
                query,
                e,
                "Returning None",
            )
            return None

    def create_querystring(self, search_text: str) -> dict[str, str]:
        return (
            {"or_subset_publication_citations": search_text}
            if self.query_subset_citations
            else {
                "search_mode": "content",
                "search_text": search_text,
                "search_type": "kws",
                "search_field": "doi"
                if search_text.startswith("10.")
                else "text_search",
            }
        )


class Style(Enum):
    """An enum that represents
    different academic writing styles.

    Parameters
    ----------
    Style : Enum
        A given academic writing style
    """

    APA = "apa"
    MLA = "modern-language-association"
    CHI = "chicago-fullnote-bibliography"


@dataclass(slots=True)
class CitationScraper(WebScraper):
    """
    CitationsScraper is a webscraper made exclusively for generating citations
    for requested papers.

    Attributes
    --------
    style : Style
        An Enum denoting a specific kind of writing style.
        Defaults to "apa".
    lang : str
        A string denoting which language will be requested.
        Defaults to "en-US".
    """

    style: Style = Style.APA
    lang: str = "en-US"

    def obtain(self, search_text: str) -> Optional[str]:
        querystring: dict[str, Any] = self.create_querystring(search_text)
        response: Response = client.get(self.url, params=querystring)
        logger.debug(
            "search_text=%s, scraper=%s, status_code=%s",
            search_text,
            repr(self),
            response.status_code,
        )
        return response.text if response.status_code == 200 else None

    def create_querystring(self, search_text) -> dict[str, Any]:
        return {
            "doi": search_text,
            "style": self.style.value,
            "lang": self.lang,
        }


@dataclass(slots=True)
class SummaryScraper(WebScraper):
    """
    SummaryScraper is a webscraper made exclusively
    for getting abstracts to papers
    within the dimensions.ai website.
    """

    def obtain(self, search_text: str) -> Optional[str]:
        url: str = f"{self.url}/{search_text}/abstract.json"
        response: Response = client.get(url)
        logger.debug(
            "search_text=%s, scraper=%s, status_code=%s",
            search_text,
            repr(self),
            response.status_code,
        )

        if response.status_code != 200:
            return None

        docs: dict[Any, Any] = loads(response.text)["docs"]
        return str(docs[0].get("abstract"))


@dataclass(slots=True)
class SemanticFigureScraper(WebScraper):
    """Scraper that queries
    semanticscholar.org for graphs and charts
    from the paper in question.
    """

    def create_querystring(self, search_text: str) -> dict[str, Any]:
        return {
            "queryString": search_text,
            "page": 1,
            "pageSize": 10,
            "sort": "relevance",
            "authors": [],
            "coAuthors": [],
            "venues": [],
            "yearFilter": None,
            "requireViewablePdf": False,
            "externalContentTypes": [],
            "fieldsOfStudy": [],
            "useFallbackRankerService": False,
            "useFallbackSearchCluster": False,
            "hydrateWithDdb": True,
            "includeTldrs": True,
            "performTitleMatch": True,
            "includeBadges": True,
            "tldrModelVersion": "v2.0.0",
            "getQuerySuggestions": False,
            "useS2FosFields": True,
        }

    def obtain(self, search_text: str) -> Optional[list[Optional[str]]]:
        payload = self.create_querystring(search_text)
        response: Response = client.post(self.url, json=payload)
        logger.debug(
            "search_text=%s, scraper=%s, status_code=%s",
            search_text,
            repr(self),
            response.status_code,
        )
        if response.status_code != 200:
            return None

        sha = self.get_paper_id(response.text)
        return self.get_semantic_images(sha) if sha else None

    def get_paper_id(self, resp_text: str) -> Optional[Any]:
        """
        Gets the SemanticScholar Paper ID by using
        a server's response to an HTTP response."""

        try:
            docs = loads(resp_text)["results"]
            return docs[0].get("id")
        except IndexError as e:
            logger.error(
                "The following error occurred in %s while getting paper ID info: %s. \
                No figures found for this entry.",
                repr(self.get_paper_id),
                e,
            )
            return None

    def get_semantic_images(
        self, semantic_scholar_id: str
    ) -> Optional[list[Optional[str]]]:
        """
        get_semantic_images takes a previously acquired SemanticScholar
        Paper ID, passes that into a `revised_url`,
        requests the webpage for the paper in question,
        and then scrapes that webpage for graphs and figures from that paper
        using Selectolax.

        Parameters
        ---------
        semantic_scholar_id : str
            A SemanticScholar Paper ID, which can be used
            to access the semanticscholar.org webpage
            for the paper, if possible.

        Returns
        ------
        Optional[list[str]] :
            A list of links to figures and images related
            to the requested paper. Otherwise, returns None.
        """

        revised_url: str = (
            f"https://www.semanticscholar.org/paper/{semantic_scholar_id}"
        )
        response: Response = client.get(revised_url)
        logger.debug(
            "sha=%s,\
            scraper=%s,\
            status_code=%s",
            semantic_scholar_id,
            repr(self),
            response.status_code,
        )
        if response.status_code != 200:
            return None

        tree: HTMLParser = HTMLParser(response.text)
        images: list[Any] = tree.css("li.figure-list__figure > a > figure > div > img")
        return [image.attributes.get("src") for image in images] if images else None
