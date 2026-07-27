from __future__ import annotations

from pathlib import Path


COMPONENT_HTML = (
    Path(__file__).parents[1]
    / "src"
    / "symnote"
    / "components"
    / "mindmap_canvas"
    / "index.html"
)


def test_component_clears_old_edges_before_redrawing() -> None:
    source = COMPONENT_HTML.read_text(encoding="utf-8")

    clear_index = source.index("edges.replaceChildren();")
    draw_index = source.index("for (const node of nodes)", clear_index)
    assert clear_index < draw_index
