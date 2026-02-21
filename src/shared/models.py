from pydantic import BaseModel, Field, field_validator


class BackstageEntityMetadata(BaseModel):
    name: str
    description: str | None = None
    annotations: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class BackstageEntitySpec(BaseModel):
    type: str | None = Field(default=None, alias="type")
    lifecycle: str | None = None
    owner: str | None = None
    system: str | None = None
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")

    model_config = {"populate_by_name": True}


class BackstageEntity(BaseModel):
    metadata: BackstageEntityMetadata
    spec: BackstageEntitySpec = Field(default_factory=BackstageEntitySpec)


class Repository(BaseModel):
    name: str
    description: str | None = None
    github_slug: str | None = None
    component_type: str | None = None
    lifecycle: str | None = None
    owner: str | None = None
    system: str | None = None
    tags: list[str] = Field(default_factory=list)

    @classmethod
    def from_entity(cls, entity: BackstageEntity) -> "Repository":
        slug = entity.metadata.annotations.get("github.com/project-slug")
        return cls(
            name=entity.metadata.name,
            description=entity.metadata.description,
            github_slug=slug,
            component_type=entity.spec.type,
            lifecycle=entity.spec.lifecycle,
            owner=entity.spec.owner,
            system=entity.spec.system,
            tags=entity.metadata.tags,
        )


class BugReport(BaseModel):
    jira_key: str
    summary: str
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    priority: str = "P3"
    reporter: str | None = None
    components: list[str] = Field(default_factory=list)
    created: str | None = None
    url: str | None = None


class TriageResult(BaseModel):
    repo: str
    confidence: float
    reasoning: str = ""

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v


def _validate_confidence(cls, v: float) -> float:  # noqa: N805
    if not 0.0 <= v <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    return v


class FileChange(BaseModel):
    path: str
    diff: str = ""


class ProposedFix(BaseModel):
    description: str = ""
    files_changed: list[FileChange] = Field(default_factory=list)


class InvestigationResult(BaseModel):
    repo: str
    root_cause_found: bool = False
    confidence: float = 0.0
    root_cause: str = ""
    evidence: list[str] = Field(default_factory=list)
    recent_suspect_commits: list[str] = Field(default_factory=list)
    proposed_fix: ProposedFix | None = None
    next_steps: list[str] = Field(default_factory=list)

    _validate_confidence = field_validator("confidence")(_validate_confidence)


class Action(BaseModel):
    action_type: str  # "pr" | "comment_root_cause" | "comment_uncertain" | "comment_summary"
    confidence: float
    has_fix: bool = False


class AggregatedFindings(BaseModel):
    bug: BugReport
    results: list[InvestigationResult] = Field(default_factory=list)
    best_result: InvestigationResult | None = None
    action: Action | None = None
