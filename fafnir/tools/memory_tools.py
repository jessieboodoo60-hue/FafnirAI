"""Tools for Fafnir to manage its own long-term memory (see fafnir/memory.py)."""
from __future__ import annotations

from ..memory import CATEGORIES, MemoryStore
from .registry import Registry, Tool, ToolError


def register(reg: Registry, store: MemoryStore) -> None:
    def remember(fact: str, category: str = "other") -> dict:
        try:
            return store.remember(fact, category)
        except ValueError as e:
            raise ToolError(str(e))

    def update(id: str, fact: str) -> dict:
        try:
            return store.update(id, fact)
        except KeyError:
            raise ToolError(f"No memory with id {id}. Use memory_search to find it.")

    def forget(id: str) -> dict:
        try:
            return store.forget(id)
        except KeyError:
            raise ToolError(f"No memory with id {id}. Use memory_search to find it.")

    def search(query: str) -> dict:
        hits = store.search(query)
        return {"results": [{"id": f.id, "fact": f.text, "category": f.category} for f in hits]}

    def describe(id: str) -> str:
        f = next((f for f in store.all() if f.id == id), None)
        return f"Forget that {f.text[0].lower() + f.text[1:]}" if f else f"Forget memory {id}"

    reg.register(Tool(
        name="memory_remember",
        description=(
            "Save one durable fact about the owner to long-term memory so you still know it in future "
            "conversations. Use when they share something lasting (name, preferences, people in their "
            "life, business details, decisions, routines) or say 'remember...'. One short, plain "
            "statement per call, written in the third person ('Prefers morning meetings.'). Don't save "
            "passing chatter, one-off requests, or anything already in memory. Don't save passwords."
        ),
        properties={
            "fact": {"type": "string"},
            "category": {"type": "string", "enum": list(CATEGORIES), "description": "Default 'other'."},
        },
        required=["fact"],
        handler=remember,
    ))
    reg.register(Tool(
        name="memory_update",
        description="Correct or update a stored fact by its id (e.g. m4) when it has changed or was wrong.",
        properties={"id": {"type": "string"}, "fact": {"type": "string", "description": "The full corrected fact."}},
        required=["id", "fact"],
        handler=update,
    ))
    reg.register(Tool(
        name="memory_forget",
        description="Delete a stored fact by its id when the owner asks you to forget it or it's no longer true.",
        properties={"id": {"type": "string"}},
        required=["id"],
        handler=forget,
        confirm_if=lambda a: describe(a["id"]) + ".",
    ))
    reg.register(Tool(
        name="memory_search",
        description="Search long-term memory for facts about a topic, including older ones not shown in your context.",
        properties={"query": {"type": "string"}},
        required=["query"],
        handler=search,
    ))
