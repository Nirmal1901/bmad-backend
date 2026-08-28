import datetime
from typing import Optional, List

from pydantic import BaseModel


class AgentOut(BaseModel):
    id: str
    name: str
    title: str
    icon: Optional[str] = None
    module: str
    base_persona_md: str
    identity: Optional[str] = None
    menu_items: list[str] = []
    suitable_roles: list[str] = ["pm", "developer"]

    class Config:
        from_attributes = True


class UserRegisterIn(BaseModel):
    username: str
    password: str
    name: Optional[str] = None
    about: Optional[str] = None
    agent_role: str = "developer"  # "pm" | "developer" — which agents this user sees


class UserLoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    name: Optional[str] = None
    about: Optional[str] = None
    agent_role: str
    created_at: datetime.datetime
    last_active_at: Optional[datetime.datetime] = None

    class Config:
        from_attributes = True


class UserUpdateIn(BaseModel):
    name: Optional[str] = None
    about: Optional[str] = None
    agent_role: Optional[str] = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ConsoleCommandIn(BaseModel):
    command: str


class ConsoleCommandOut(BaseModel):
    command: str
    stdout: str
    stderr: str
    exit_code: int


class ProfileIn(BaseModel):
    name: str
    about: Optional[str] = None
    role: str = "developer"  # "pm" | "developer"


class ProfileOut(BaseModel):
    id: int
    name: str
    about: Optional[str] = None
    role: str
    created_at: datetime.datetime
    last_active_at: Optional[datetime.datetime] = None

    class Config:
        from_attributes = True


class BRDUploadIn(BaseModel):
    name: Optional[str] = "Untitled Pipeline"
    brd_text: str


class PipelineCreateIn(BaseModel):
    name: Optional[str] = "Untitled Pipeline"
    brd_text: Optional[str] = None
    agent_ids: List[str] = []   # ordered list -> becomes ordered nodes


class NodeOut(BaseModel):
    id: int
    agent_id: str
    order_index: int
    persona_md: str
    status: str
    last_input_text: Optional[str] = None
    output_text: Optional[str] = None
    updated_at: datetime.datetime
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    selected_menu_item: Optional[str] = None
    max_tokens: Optional[int] = None

    class Config:
        from_attributes = True


class NodeMaxTokensIn(BaseModel):
    max_tokens: Optional[int] = None  # null clears it back to the router default (4096)


class NodePositionIn(BaseModel):
    x: float
    y: float


class NodeTaskIn(BaseModel):
    menu_item: Optional[str] = None  # null clears it back to "auto"


class PipelineOut(BaseModel):
    id: int
    name: str
    brd_text: Optional[str] = None
    owner_id: Optional[int] = None
    owner_username: Optional[str] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    nodes: List[NodeOut] = []

    class Config:
        from_attributes = True


class NodePersonaUpdateIn(BaseModel):
    persona_md: str


class NodeReorderIn(BaseModel):
    node_id: int
    new_index: int


class NodeAddIn(BaseModel):
    agent_id: str
    order_index: Optional[int] = None   # append at end if omitted
    position_x: Optional[float] = None  # drop point on the canvas, if known
    position_y: Optional[float] = None


class RunResultOut(BaseModel):
    node: NodeOut
    stale_node_ids: List[int]
    artifact_id: Optional[int] = None


class ArtifactOut(BaseModel):
    id: int
    pipeline_id: int
    node_id: int
    title: Optional[str] = None
    content_md: str
    created_at: datetime.datetime

    class Config:
        from_attributes = True


class EpicIn(BaseModel):
    title: str
    content_md: str
    source_artifact_id: Optional[int] = None


class EpicsCreateIn(BaseModel):
    epics: List[EpicIn]


class SuggestEpicsIn(BaseModel):
    artifact_id: int


class EpicOut(BaseModel):
    id: int
    pipeline_id: int
    source_artifact_id: Optional[int] = None
    title: str
    content_md: str
    order_index: int
    created_at: datetime.datetime
    status: str = "pending"
    status_message: Optional[str] = None
    updated_at: Optional[datetime.datetime] = None

    class Config:
        from_attributes = True


class ConfigOut(BaseModel):
    yaml: str


class DocumentOut(BaseModel):
    id: int
    title: str
    category: str
    scope: str
    node_id: Optional[int] = None
    pipeline_id: Optional[int] = None
    source_filename: Optional[str] = None
    applicable_roles: list[str] = ["pm", "developer"]
    chunk_count: int
    created_at: datetime.datetime

    class Config:
        from_attributes = True
