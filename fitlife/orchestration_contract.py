"""Server-owned routing bindings; never grant a child's foreign tool permission."""

from dataclasses import dataclass

from pydantic import Field, ValidationError

from fitlife.business import Input
from fitlife.intent_constraints import activity_logging_scope
from fitlife.registry import DOMAIN_READ_TOOLS, DOMAIN_TOOLS
from fitlife.routing import Plan, Requirement, Requirements, check_coverage

VERSION = "intent-bind-v2.4"


class ContractError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code, self.details = code, details


class RawInventory(Input):
    requirements: list[Requirement] = Field(min_length=1, max_length=12)


@dataclass
class BoundInventory:
    inventory: Requirements
    evidence_edges: dict
    bindings: list

    @property
    def requirements(self):
        return self.inventory.requirements

    def model_dump(self):
        return self.inventory.model_dump()


def bind_request_inventory(data, message):
    """Bind clear activity logging to its actual request, not a router's query label.

    This only removes known exercise tools. Unknown/foreign tools still reach the
    strict binder and fail; no record values or missing facts are manufactured.
    """
    scope = activity_logging_scope(message)
    if scope is None:
        return bind_inventory(data)
    raw = RawInventory.model_validate(data)
    requirements, changes = [], []
    for req in raw.requirements:
        if req.domain == "exercise":
            removed = sorted(set(req.required_tools) & (DOMAIN_TOOLS["exercise"] - scope))
            reads = [
                t
                for t in req.required_tools
                if t not in DOMAIN_TOOLS["exercise"] or t in scope & DOMAIN_READ_TOOLS
            ]
            if removed or req.kind != "deliverable" or reads != req.required_tools:
                changes.append(
                    {
                        "kind": "activity_logging_request_binding",
                        "removed_reads": removed,
                        "router_kind": req.kind,
                        "bound_kind": "deliverable",
                    }
                )
            req = req.model_copy(update={"kind": "deliverable", "required_tools": reads})
        requirements.append(req)
    bound = bind_inventory({"requirements": [r.model_dump() for r in requirements]})
    bound.bindings.extend(changes)
    return bound


def bind_inventory(data):
    raw = RawInventory.model_validate(data)
    edges, bindings, bound = {}, [], []
    for req in raw.requirements:
        reads = []
        for tool in req.required_tools:
            if tool in DOMAIN_TOOLS.get(req.domain, ()):
                if tool in DOMAIN_READ_TOOLS:
                    reads.append(tool)
                elif req.kind == "deliverable":
                    bindings.append(
                        {"kind": "write_not_read_dependency", "domain": req.domain, "tool": tool}
                    )
                else:
                    raise ContractError("QUERY_REQUIRES_WRITE_TOOL", domain=req.domain)
                continue
            owners = [domain for domain, tools in DOMAIN_TOOLS.items() if tool in tools]
            # A foreign read is satisfied only by an already explicit owner task with
            # the same required read. Unknown tools, foreign writes and new domains fail.
            if (
                req.domain != "main"
                and tool in DOMAIN_READ_TOOLS
                and len(owners) == 1
                and any(
                    r.domain == owners[0] and tool in r.required_tools for r in raw.requirements
                )
            ):
                owner = owners[0]
                edges.setdefault(req.domain, [])
                if owner not in edges[req.domain]:
                    edges[req.domain].append(owner)
                bindings.append(
                    {"kind": "parent_evidence", "domain": req.domain, "source": owner, "tool": tool}
                )
            else:
                raise ContractError(
                    "TOOL_REQUIRES_EXPLICIT_OWNER_READ",
                    domain=req.domain,
                    tool=tool if owners else "UNKNOWN_TOOL",
                    owners=owners,
                )
        bound.append(req.model_copy(update={"required_tools": list(dict.fromkeys(reads))}))
    inventory = Requirements(requirements=bound)
    return BoundInventory(inventory, edges, bindings)


class DependencyDraft(Input):
    tasks: dict[str, str]
    depends_on: dict[str, list[str]] = Field(default_factory=dict)
    # Legacy planner replies remain readable, but server bindings are authoritative.
    agents: list[str] | None = None
    covers: dict[str, list[str]] | None = None


def bind_plan(data, inventory):
    draft = DependencyDraft.model_validate(data)
    reqs = inventory.requirements
    domains = list(dict.fromkeys(r.domain for r in reqs if r.domain != "main"))
    has_main = any(r.domain == "main" for r in reqs)
    allowed = set(domains) | ({"main"} if has_main else set())
    if set(draft.tasks) - allowed or set(draft.tasks) - {"main"} != set(domains):
        raise ContractError("PLAN_TASK_DOMAINS_MUST_MATCH_INVENTORY", domains=domains)
    if draft.agents is not None and (
        len(draft.agents) != len(set(draft.agents))
        or set(draft.agents) - allowed
        or set(draft.agents) - {"main"} != set(domains)
    ):
        raise ContractError("PLAN_AGENT_DOMAINS_MUST_MATCH_INVENTORY", domains=domains)
    if set(draft.depends_on) - allowed:
        raise ContractError("PLAN_HAS_UNKNOWN_DEPENDENCY_DOMAIN", domains=domains)
    if any(set(parents) - set(domains) for parents in draft.depends_on.values()):
        raise ContractError("PARENT_MUST_BE_EXECUTABLE_DOMAIN", domains=domains)
    dependencies = {domain: list(draft.depends_on.get(domain, [])) for domain in domains}
    for domain, parents in inventory.evidence_edges.items():
        for parent in parents:
            if parent not in dependencies[domain]:
                dependencies[domain].append(parent)
    covers = {
        domain: [r.id for r in reqs if r.domain == domain]
        for domain in [*domains, "main"]
        if any(r.domain == domain for r in reqs)
    }
    plan = Plan(
        agents=domains,
        tasks={d: draft.tasks[d] for d in domains},
        depends_on=dependencies,
        covers=covers,
    )
    check_coverage(plan, reqs)
    if draft.covers is not None:
        check_coverage(plan.model_copy(update={"covers": draft.covers}), reqs)
    return plan


def repair_feedback(exc, stage):
    # Only catalogue values and fixed schema facts. Never emit Pydantic input/ctx,
    # arbitrary exception messages or unknown model-supplied tool identifiers.
    if isinstance(exc, ContractError):
        return {"stage": stage, "code": exc.code, **exc.details}
    if isinstance(exc, ValidationError):
        return {
            "stage": stage,
            "code": "INVALID_SCHEMA",
            "error_types": sorted({e["type"] for e in exc.errors()})[:8],
            "domains": list(DOMAIN_TOOLS),
            "required_kind": ["query", "deliverable"],
        }
    return {
        "stage": stage,
        "code": "INVALID_GRAPH_OR_COVERAGE",
        "rule": "No cycles; main only synthesizes; cover every task once.",
    }
