"""Tests for the verdict_gate wiring in eltm_callback.

CLASS-H structural fix verification: every game ELTM finishes building
must clear the persona gate before being marked 'completed'.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def fake_redis():
    """Mock async redis with hset/hgetall."""
    r = AsyncMock()
    state: dict[str, dict] = {}

    async def hset(key, *args, mapping=None, **kwargs):
        if key not in state:
            state[key] = {}
        if mapping:
            state[key].update({k: str(v) for k, v in mapping.items()})
        elif len(args) >= 2:
            # hset(key, field, value)
            state[key][args[0]] = str(args[1])
        elif len(args) == 1 and isinstance(args[0], dict):
            state[key].update({k: str(v) for k, v in args[0].items()})
        return 1

    async def hgetall(key):
        return state.get(key, {})

    r.hset.side_effect = hset
    r.hgetall.side_effect = hgetall
    r._state = state
    return r


@pytest.fixture(autouse=True)
def reload_callback_module(monkeypatch, tmp_path):
    """Re-import callback module with patched env so module-level constants pick up our values."""
    monkeypatch.setenv("ELTM_GAME_ROOT", str(tmp_path))
    monkeypatch.setenv("VERDICT_GATE_THRESHOLD", "60")
    monkeypatch.setenv("VERDICT_GATE_MIN_FLOOR", "30")
    monkeypatch.setenv("VERDICT_GATE_PERSONAS",
                       "aminah_first_time_merchant,skeptical_owner,consumer")

    import importlib
    from app.routers import eltm_callback
    importlib.reload(eltm_callback)
    yield eltm_callback


@pytest.mark.asyncio
async def test_finished_with_passing_html_marks_completed(reload_callback_module, fake_redis, tmp_path):
    cb = reload_callback_module
    game_html = "<html><body>" + "x" * 3000 + "</body></html>"
    game_file = tmp_path / "kopi" / "spin.html"
    game_file.parent.mkdir(parents=True)
    game_file.write_text(game_html)

    fake_redis._state["game_order:abc"] = {"status": "building"}

    fake_request = MagicMock()
    fake_request.headers.get.return_value = ""

    body = {
        "order_id": "abc",
        "event": "finished",
        "result": {
            "ok": True,
            "relative_path": "kopi/spin.html",
            "game_name": "Kopi Spin",
            "game_slug": "kopi_spin",
            "elapsed_s": 12,
        },
    }
    out = await cb.eltm_callback(body, fake_request, r=fake_redis)
    assert out["status"] == "completed"
    final = fake_redis._state["game_order:abc"]
    assert final["status"] == "completed"
    assert final["verdict_status"] == "accepted"
    assert float(final["verdict_avg"]) >= 60


@pytest.mark.asyncio
async def test_finished_with_failing_html_marks_failed(reload_callback_module, fake_redis, tmp_path):
    cb = reload_callback_module
    # Trip stub_evaluator: TODO + tiny output → all personas score low
    game_html = "<html>TODO TODO {{x}}</html>"
    game_file = tmp_path / "bad.html"
    game_file.write_text(game_html)

    fake_redis._state["game_order:bad"] = {"status": "building"}
    fake_request = MagicMock()
    fake_request.headers.get.return_value = ""

    body = {
        "order_id": "bad",
        "event": "finished",
        "result": {
            "ok": True,
            "relative_path": "bad.html",
            "game_name": "Bad Game",
        },
    }
    out = await cb.eltm_callback(body, fake_request, r=fake_redis)
    assert out["status"] == "failed"
    final = fake_redis._state["game_order:bad"]
    assert final["status"] == "failed"
    assert final["verdict_status"] == "rejected"
    assert "verdict_gate rejected" in final["error"]


@pytest.mark.asyncio
async def test_finished_with_no_file_doesnt_block(reload_callback_module, fake_redis):
    """File-not-found at ELTM_GAME_ROOT → gate marked 'no-file', game still completes
    (don't punish ELTM for callback racing the file write — log + ship)."""
    cb = reload_callback_module
    fake_redis._state["game_order:miss"] = {"status": "building"}
    fake_request = MagicMock()
    fake_request.headers.get.return_value = ""

    body = {
        "order_id": "miss",
        "event": "finished",
        "result": {
            "ok": True,
            "relative_path": "nonexistent/x.html",
            "game_name": "Missing",
        },
    }
    out = await cb.eltm_callback(body, fake_request, r=fake_redis)
    assert out["status"] == "completed"
    final = fake_redis._state["game_order:miss"]
    assert final["verdict_status"] == "no-file"


@pytest.mark.asyncio
async def test_progress_event_unchanged(reload_callback_module, fake_redis):
    cb = reload_callback_module
    fake_redis._state["game_order:p"] = {"status": "building"}
    fake_request = MagicMock()
    fake_request.headers.get.return_value = ""

    out = await cb.eltm_callback(
        {"order_id": "p", "event": "progress", "message": "step 3 of 8"},
        fake_request, r=fake_redis,
    )
    assert out["status"] == "building"
    assert fake_redis._state["game_order:p"]["progress_message"] == "step 3 of 8"
