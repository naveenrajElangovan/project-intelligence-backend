from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JiraProjectSource:
    site_url: str
    project_key: str


@dataclass(frozen=True, slots=True)
class ConfluenceSpaceSource:
    site_url: str
    space_key: str
    space_id: str
    root_page_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GitHubRepositorySource:
    owner: str
    repository: str
    indexed_branches: tuple[str, ...] = ("main",)
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


@dataclass(frozen=True, slots=True)
class VectorStoreRoute:
    collection_name: str = "project-intelligence"
    text_field: str = "chunk_text"
    embedding_field: str = "embedding_text"
    embedding_model: str = "multilingual-e5-large"
    schema_version: str = "3"


@dataclass(frozen=True, slots=True)
class IngestionSchedule:
    github_merged_pr_enabled: bool = True
    daily_enabled: bool = True
    daily_at: str = "00:00"
    timezone: str = "America/Mexico_City"
    manual_enabled: bool = True


@dataclass(frozen=True, slots=True)
class SourceAccessRule:
    provider: str
    match_field: str
    prefix: str
    access_policy_id: str


@dataclass(frozen=True, slots=True)
class RetrievalProfile:
    max_chunks_per_source: int
    rerank_top_n: int
    mixed_source_top_n: int
    rerank_score_threshold: float = 0.10

    def as_payload(self) -> dict[str, int | float]:
        return {
            "maxChunksPerSource": self.max_chunks_per_source,
            "rerankTopN": self.rerank_top_n,
            "mixedSourceTopN": self.mixed_source_top_n,
            "rerankScoreThreshold": self.rerank_score_threshold,
        }


@dataclass(frozen=True, slots=True)
class ProjectDefinition:
    project_id: str
    display_name: str
    active: bool
    jira_projects: tuple[JiraProjectSource, ...]
    confluence_spaces: tuple[ConfluenceSpaceSource, ...]
    github_repositories: tuple[GitHubRepositorySource, ...]
    vector_store: VectorStoreRoute
    ingestion_schedule: IngestionSchedule = IngestionSchedule()
    source_access_rules: tuple[SourceAccessRule, ...] = ()
    retrieval_profile: RetrievalProfile | None = None

    def github_repository(self, owner: str, repository: str) -> GitHubRepositorySource | None:
        expected = f"{owner}/{repository}".lower()
        return next(
            (source for source in self.github_repositories if source.full_name.lower() == expected),
            None,
        )
