from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from eviforge.automation import RunResult
from eviforge.lifecycle import Lifecycle


@pytest.mark.asyncio
async def test_lifecycle_close_from_owned_task_does_not_await_itself():
    owner = Lifecycle()
    child_started, child_finished = asyncio.Event(), asyncio.Event()
    async def child():
        child_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            child_finished.set()
    owner.create_task(child(), name="worker")
    await child_started.wait()
    async def shutdown():
        await owner.cancel()
        return "closed"
    closing = owner.create_task(shutdown(), name="owner-shutdown")
    assert await asyncio.wait_for(closing, 1) == "closed"
    assert child_finished.is_set() and not owner.pending()


def test_public_schema_validates_real_result_and_rejects_unknown_version():
    from importlib.resources import files
    schema = json.loads(files("eviforge").joinpath("schemas/run-result-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    result = RunResult.failure("ambiguous", "AmbiguousStreamError", "partial stream", output="partial")
    validator.validate(result.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        validator.validate(result.model_dump(mode="json") | {"schema_version": "99.0"})
    with pytest.raises(ValidationError):
        validator.validate(result.model_dump(mode="json") | {"unversioned_extra": True})


def test_registered_plugin_agent_source_loads_and_respects_project_priority(tmp_path, monkeypatch):
    from eviforge.agents.loader import AgentLoader
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    text = "---\nname: PluginReader\ndescription: plugin reader\ntools: [ReadFile]\n---\nPlugin instruction"
    (plugin / "reader.md").write_text(text)
    loader = AgentLoader(str(tmp_path))
    loader.register_plugin_source(plugin)
    assert loader.get("PluginReader").source == "plugin"
    project = tmp_path / ".eviforge/agents"
    project.mkdir(parents=True)
    (project / "reader.md").write_text(text.replace("Plugin instruction", "Project instruction"))
    assert loader.get("PluginReader").source == "project"
    assert loader.get("PluginReader").system_prompt == "Project instruction"
