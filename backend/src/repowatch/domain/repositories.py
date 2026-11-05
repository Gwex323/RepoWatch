import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repowatch.application.credentials import validate_reference


class AdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str | None = None
    allowed_labels: list[str] = Field(default_factory=list, max_length=100)
    excluded_labels: list[str] = Field(default_factory=list, max_length=100)
    severity_labels: dict[str, int] = Field(default_factory=dict, max_length=100)
    claim_comment: bool = Field(default=False, strict=True)
    claim_template: str = Field(default="RepoWatch 将按已确认范围处理此 Issue。", max_length=4000)

    @model_validator(mode="after")
    def validate_policy(self):
        if self.mode not in {None, "manual", "labels", "all_bugs"}:
            raise ValueError("准入模式无效")
        labels = self.allowed_labels + self.excluded_labels + list(self.severity_labels)
        if any(not label.strip() or len(label) > 100 for label in labels):
            raise ValueError("标签须为有效名称")
        if any(
            type(level) is not int or not 0 <= level <= 4 for level in self.severity_labels.values()
        ):
            raise ValueError("严重程度必须在 0—4 内")
        if set(self.allowed_labels) & set(self.excluded_labels):
            raise ValueError("允许与排除标签不能重叠")
        if self.claim_comment and not self.claim_template.strip():
            raise ValueError("启用认领评论须提供模板")
        return self


class ScanPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_bytes: int = Field(default=16000, strict=True, ge=256, le=64000)
    exclusions: dict[str, str] = Field(default_factory=dict, max_length=1000)

    @field_validator("exclusions")
    @classmethod
    def valid_exclusions(cls, value):
        for path, reason in value.items():
            if (
                path.startswith("/")
                or "\\" in path
                or any(part in {"", ".", "..", ".git"} for part in path.split("/"))
            ):
                raise ValueError("排除路径无效")
            if not reason.strip() or len(reason) > 1000:
                raise ValueError("排除项必须记录有效依据")
        return value


class RepositoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str
    name: str
    target_branch: str | None = Field(default=None, max_length=250)
    contribution_path: str = "direct"
    fork_owner: str | None = None
    fork_name: str | None = None
    credential_reference: str | None = None

    @field_validator("credential_reference")
    @classmethod
    def valid_reference(cls, value):
        if value is not None:
            validate_reference(value)
        return value

    @field_validator("owner", "name", "fork_owner", "fork_name")
    @classmethod
    def valid_identifier(cls, value):
        if value is not None and (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) or value in {".", ".."}
        ):
            raise ValueError("身份或凭据引用无效")
        return value

    @model_validator(mode="after")
    def valid_path(self):
        if self.credential_reference is None:
            raise ValueError("仓库预检需要宿主 GitHub 凭据引用")
        if self.contribution_path not in {"direct", "fork"}:
            raise ValueError("贡献路径无效")
        if self.contribution_path == "fork" and not (self.fork_owner and self.fork_name):
            raise ValueError("Fork 路径须指定发布仓库")
        if self.target_branch is not None and (
            not self.target_branch.strip()
            or self.target_branch.startswith("-")
            or self.target_branch.endswith(("/", ".", ".lock"))
            or ".." in self.target_branch
            or "@{" in self.target_branch
            or any(char in self.target_branch for char in " ~^:?*[\\")
            or any(ord(char) < 32 for char in self.target_branch)
        ):
            raise ValueError("目标分支无效")
        return self
