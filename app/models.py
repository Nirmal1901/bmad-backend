"""
Schema (matches the design we agreed on):

pipelines        - a saved linear chain of agent nodes
pipeline_nodes   - one agent instance in a pipeline, with its own editable
                   persona override, run status, and output
agents           - agent definitions discovered from the _bmad folder
artifacts        - rendered/presentable output produced when a node runs
"""
import datetime
import enum

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Enum, Float
)
from sqlalchemy.orm import relationship

from app.database import Base


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class User(Base):
    """Real login account. Replaces the old no-password Profile as the
    thing you sign in as — name/about/agent_role still drive which
    agents show up in the canvas (pm vs developer), role drives
    whether you get the admin views."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.user, nullable=False)
    name = Column(String, nullable=True)
    about = Column(Text, nullable=True)
    agent_role = Column(String, nullable=False, default="developer")  # "pm" | "developer"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.datetime.utcnow,
                             onupdate=datetime.datetime.utcnow)


class NodeStatus(str, enum.Enum):
    not_run = "not_run"
    fresh = "fresh"
    stale = "stale"
    running = "running"
    error = "error"


class EpicStatus(str, enum.Enum):
    pending = "pending"     # created, not yet sent to Aider/Stakpak
    running = "running"     # actively being worked (Aider and/or Stakpak stage)
    done = "done"           # finished successfully — the "done" signal
    error = "error"         # failed; see execution log for details


class Agent(Base):
    __tablename__ = "agents"

    id = Column(String, primary_key=True)          # e.g. "bmm-analyst"
    name = Column(String, nullable=False)           # e.g. "Mary"
    title = Column(String, nullable=False)          # e.g. "Business Analyst"
    icon = Column(String, nullable=True)
    module = Column(String, nullable=False)         # e.g. "bmm"
    source_path = Column(String, nullable=False)     # path under _bmad/
    base_persona_md = Column(Text, nullable=False)   # full original persona file
    # What this agent actually does day-to-day, parsed straight from its
    # persona file, so the UI can show it before someone drags the agent
    # onto the canvas. "Developer Agent" alone doesn't tell you whether
    # it writes stories or only implements ones that already exist.
    identity = Column(Text, nullable=True)
    menu_items_json = Column(Text, nullable=True)   # JSON list[str]
    # Which profile roles see this agent in the canvas sidebar. A PM
    # profile shouldn't be staring at code-implementation agents they'll
    # never use; a Developer profile sees everything. Computed once at
    # sync time from the agent's slug (see bmad_loader.ROLE_RESTRICTED).
    suitable_roles_json = Column(Text, nullable=True)  # JSON list[str]

    @property
    def menu_items(self) -> list:
        import json
        try:
            return json.loads(self.menu_items_json) if self.menu_items_json else []
        except Exception:
            return []

    @property
    def suitable_roles(self) -> list:
        import json
        try:
            return json.loads(self.suitable_roles_json) if self.suitable_roles_json else ["pm", "developer"]
        except Exception:
            return ["pm", "developer"]


class Profile(Base):
    """A lightweight local profile — no password, no session token. This
    app runs single-user on SQLite; a profile is just "who's using it
    right now" so the UI can show the right set of agents (PM vs
    Developer) and, later, remember preferences per person."""
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    about = Column(Text, nullable=True)
    role = Column(String, nullable=False, default="developer")  # "pm" | "developer"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.datetime.utcnow,
                             onupdate=datetime.datetime.utcnow)


class Pipeline(Base):
    __tablename__ = "pipelines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, default="Untitled Pipeline")
    brd_text = Column(Text, nullable=True)
    # Who this "session" belongs to. Nullable so pipelines created
    # before auth existed don't break; treated as unowned/legacy and
    # only reachable by an admin from that point on.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                         onupdate=datetime.datetime.utcnow)

    nodes = relationship(
        "PipelineNode", back_populates="pipeline",
        order_by="PipelineNode.order_index",
        cascade="all, delete-orphan"
    )
    owner = relationship("User")

    @property
    def owner_username(self) -> str | None:
        return self.owner.username if self.owner else None


class PipelineNode(Base):
    __tablename__ = "pipeline_nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"), nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    order_index = Column(Integer, nullable=False)

    # Free-form canvas position — purely visual, doesn't affect
    # execution order (order_index still governs the context chain).
    # Nullable so old rows fall back to an auto-layout on first load.
    position_x = Column(Float, nullable=True)
    position_y = Column(Float, nullable=True)

    # Which of the agent's real menu items to run, e.g. "[CS] Context
    # Story: ...". Null means "let the agent pick the closest fit"
    # (the old behavior). Explicit selection is what the persona
    # dropdown in NodePanel writes to instead of hand-editing markdown.
    selected_menu_item = Column(Text, nullable=True)

    # Editable live persona override. Starts as a copy of the agent's
    # base_persona_md; user can edit this per-pipeline without touching
    # the original agent definition.
    persona_md = Column(Text, nullable=False)

    # Output token ceiling for this node's LLM call. Null = fall back
    # to the router default (4096). This is a hard ceiling only — the
    # Anthropic/OpenAI-compatible/Ollama APIs have no "minimum length"
    # concept, so a floor (e.g. "at least 300 words") has to be
    # enforced via persona instructions instead, not this field.
    max_tokens = Column(Integer, nullable=True)

    status = Column(Enum(NodeStatus), default=NodeStatus.not_run, nullable=False)
    last_input_text = Column(Text, nullable=True)  # exact prompt sent on the last run — for the "how was this formed" transcript
    output_text = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                         onupdate=datetime.datetime.utcnow)

    pipeline = relationship("Pipeline", back_populates="nodes")
    agent = relationship("Agent")


class Epic(Base):
    """A real, individually-implementable unit of work — extracted from
    a Dev Story's task list (e.g. "Task 3: Implement mock auth
    service"), NOT a whole artifact. This is what actually gets sent to
    Aider one at a time; every Artifact in the pipeline still comes
    along as knowledge base (see execution.py:_epic_context)."""
    __tablename__ = "epics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"), nullable=False)
    source_artifact_id = Column(Integer, ForeignKey("artifacts.id"), nullable=True)
    title = Column(String, nullable=False)
    content_md = Column(Text, nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Execution status — the "done" signal for Screen 4/5. Set by the
    # execution worker: pending -> running -> done (or error). Persisted
    # so a page reload/revisit still shows what actually finished.
    status = Column(Enum(EpicStatus), default=EpicStatus.pending, nullable=False)
    status_message = Column(Text, nullable=True)  # last status detail, e.g. error text
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                         onupdate=datetime.datetime.utcnow)


class Document(Base):
    """Admin-uploaded knowledge base source: Regulatory Compliance,
    Dev/Testing Guidelines, Data Glossary, or a free-form 'custom'
    category. Two scopes:

    - scope="global": visible pipeline-wide, filtered to agents whose
      role is in applicable_roles (mirrors Agent.suitable_roles).
    - scope="node": attached to exactly one pipeline_node — e.g. extra
      context pasted in while building out a specific BRD/PM node.
      node_id/pipeline_id are set; applicable_roles is ignored (the
      attachment itself is the scoping).

    The actual chunked+embedded text lives in Chroma (see
    app/knowledge_base.py), keyed by this row's id. This table is just
    the catalog: what was uploaded, by whom (scope), and how many
    chunks it produced.
    """
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    category = Column(String, nullable=False, default="custom")
    # "regulatory_compliance" | "dev_testing_guidelines" | "data_glossary" | "custom"
    scope = Column(String, nullable=False, default="global")  # "global" | "node"
    node_id = Column(Integer, ForeignKey("pipeline_nodes.id"), nullable=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"), nullable=True)
    source_filename = Column(String, nullable=True)
    applicable_roles_json = Column(Text, nullable=True)  # JSON list[str], global docs only
    chunk_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    @property
    def applicable_roles(self) -> list:
        import json
        try:
            return json.loads(self.applicable_roles_json) if self.applicable_roles_json else ["pm", "developer"]
        except Exception:
            return ["pm", "developer"]


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"), nullable=False)
    node_id = Column(Integer, ForeignKey("pipeline_nodes.id"), nullable=False)
    title = Column(String, nullable=True)
    content_md = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
