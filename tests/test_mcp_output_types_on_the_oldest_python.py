"""A tool's answer type is one the oldest supported Python can read.

The MCP SDK turns the TypedDict a tool returns into a model, reading its
fields with `typing.get_type_hints`. From Python 3.11 that call takes
`NotRequired[...]` off a field; on 3.10 it does not know the qualifier and
leaves it there, and the model is refused. `griot_stats` and
`griot_index_status` each had one such field, so on Python 3.10, the floor
the package declares, importing the server failed and `griot mcp` did not
start at all. Every test passed, on a newer Python.

A TypedDict nested inside the answer is read by pydantic itself, which
knows the qualifier on every version: only the answer's own fields matter.
A field that may be absent is declared in a `total=False` base instead."""

import inspect
import typing

import pytest
import typing_extensions

from griot import mcp_server

QUALIFIERS = {typing_extensions.NotRequired, typing_extensions.Required,
              getattr(typing, "NotRequired", None), getattr(typing, "Required", None)} - {None}


def _the_typed_dict(answer):
    """The TypedDict the SDK builds its model from, if the answer is one:
    it looks through `Annotated[...]` first, exactly as it does here."""
    while typing_extensions.get_origin(answer) is typing_extensions.Annotated:
        answer = typing_extensions.get_args(answer)[0]
    return answer if typing_extensions.is_typeddict(answer) else None


def _qualified_fields(answer) -> list:
    fields = typing_extensions.get_type_hints(_the_typed_dict(answer), include_extras=True)
    return [name for name, annotation in fields.items() if typing_extensions.get_origin(annotation) in QUALIFIERS]


def _answer_types():
    found = {}
    for name, tool in inspect.getmembers(mcp_server, callable):
        if not name.startswith("griot_"):
            continue
        answer = typing_extensions.get_type_hints(inspect.unwrap(tool), include_extras=True).get("return")
        if _the_typed_dict(answer) is not None:
            found[name] = _the_typed_dict(answer)
    return found


def test_there_are_answer_types_to_check():
    assert len(_answer_types()) >= 15


@pytest.mark.parametrize("tool,answer", sorted(_answer_types().items()))
def test_no_field_of_an_answer_carries_a_qualifier_the_oldest_python_leaves_in_place(tool, answer):
    qualified = _qualified_fields(answer)
    assert qualified == [], f"{tool} -> {answer.__name__}: declare {qualified} in a total=False base instead"


class _Qualified(typing_extensions.TypedDict):
    always: int
    sometimes: typing_extensions.NotRequired[str]
    insisted: typing_extensions.Required[str]


class _MayBeAbsent(typing_extensions.TypedDict, total=False):
    sometimes: str


class _Declared(_MayBeAbsent):
    always: int


def test_the_check_sees_what_it_is_there_to_see():
    """On shapes of its own, so that it is not the two fields above being
    gone that makes it pass."""
    assert _qualified_fields(_Qualified) == ["sometimes", "insisted"]
    assert _qualified_fields(typing_extensions.Annotated[_Qualified, "described"]) == ["sometimes", "insisted"]
    assert _qualified_fields(_Declared) == [] and _Declared.__optional_keys__ == {"sometimes"}
    # An answer that is not a TypedDict of its own is wrapped by the SDK and
    # read by pydantic, which knows the qualifiers on every version.
    assert _the_typed_dict(typing.Optional[_Qualified]) is None and _the_typed_dict(str) is None


@pytest.mark.parametrize("tool", ["griot_stats", "griot_index_status"])
def test_the_reason_the_point_count_is_missing_may_still_be_absent(tool):
    """What NotRequired was there for: an answer without the key is an
    answer with no reason given, not a rejected one."""
    answer = _answer_types()[tool]
    assert "points_error" in answer.__optional_keys__ and "points_count" in answer.__required_keys__


def test_the_schema_a_client_sees_does_not_require_it():
    import asyncio

    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    for name in ("griot_stats", "griot_index_status"):
        schema = tools[name].output_schema
        assert "points_error" in schema["properties"] and "points_error" not in schema.get("required", [])
        assert "points_count" in schema["required"]
