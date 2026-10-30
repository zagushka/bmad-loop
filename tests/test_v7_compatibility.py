"""Compatibility checks use only fake renderers and disposable worktrees."""

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from conftest import git, install_build_auto_skill, install_render_probe_fixture

from bmad_loop import cli, install, legacy_runtime, policy, worktree_flow
from bmad_loop.adapters.profile import get_profile


def seal(root: Path) -> Path:
    files = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    (root / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    return root


def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    skills = install_build_auto_skill(root)
    (skills / "bmad-build-auto/SKILL.md").write_text("legacy workflow\n", encoding="utf-8")
    script = root / "_bmad/scripts/render_skill.py"
    script.parent.mkdir(parents=True)
    script.write_text("# preserved renderer\n", encoding="utf-8")
    return seal(root)


@pytest.mark.parametrize("fault", ["altered", "extra", "missing", "symlink", "manifest"])
def test_runtime_verifier_fails_closed(tmp_path, fault):
    runtime = bundle(tmp_path)
    legacy_runtime.verify(runtime)
    target = runtime / "_bmad/scripts/render_skill.py"
    if fault == "altered":
        target.write_text("changed", encoding="utf-8")
    elif fault == "extra":
        (runtime / "extra").write_text("extra", encoding="utf-8")
    elif fault == "missing":
        target.unlink()
    elif fault == "symlink":
        original = target.read_bytes()
        target.unlink()
        (tmp_path / "outside").write_bytes(original)
        target.symlink_to(tmp_path / "outside")
    else:
        (runtime / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(legacy_runtime.RuntimeError, match="legacy runtime"):
        legacy_runtime.verify(runtime)


def test_runtime_selection_uses_explicit_persisted_source_and_live_configuration(project, tmp_path):
    runtime = bundle(tmp_path)
    pol = dataclasses.replace(
        policy.Policy(), stories=policy.StoriesPolicy(source="tickets", legacy_runtime=str(runtime))
    )
    assert legacy_runtime.selected(project.project, pol, "tickets") is None
    assert legacy_runtime.selected(project.project, pol, "stories") == runtime
    config = project.project / "_bmad/config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("[core]\nname = 'live'\n", encoding="utf-8")
    with legacy_runtime.surface(project.project, runtime) as composed:
        assert (composed / "_bmad/config.toml").read_bytes() == config.read_bytes()
        assert (
            composed / ".claude/skills/bmad-build-auto/SKILL.md"
        ).read_text() == "legacy workflow\n"
    assert not composed.exists()
    assert not (runtime / "_bmad/config.toml").exists()


@pytest.mark.parametrize(
    "snapshot,output,rc,verdict",
    [
        ("workflow\nHALT: quoted example\n", "workflow\nHALT: quoted example\n", 0, "ok"),
        ("workflow\n", "arbitrary\n", 0, "problem"),
        (None, "workflow\n", 0, "problem"),
        ("workflow\n", "workflow\n", 1, "problem"),
    ],
)
def test_v7_renderer_requires_fresh_matching_snapshot(tmp_path, snapshot, output, rc, verdict):
    root = tmp_path / "project"
    root.mkdir()
    body = ""
    if snapshot is not None:
        body += f"destination = root / '_bmad/render/build/generation/workflow.md'\ndestination.parent.mkdir(parents=True, exist_ok=True)\ndestination.write_text({snapshot!r}, encoding='utf-8')\n"
    body += f"sys.stdout.write({output!r})\nraise SystemExit({rc})\n"
    install_render_probe_fixture(root, tmp_path / "marker", body)
    # Stale output in the real project must not satisfy the probe.
    stale = root / "_bmad/render/stale/workflow.md"
    stale.parent.mkdir(parents=True)
    stale.write_text(output, encoding="utf-8")
    [finding] = install.dev_renderer_probe(root, [".claude/skills"])
    assert finding.severity == verdict


def dependencies(root: Path) -> None:
    tree = root / ".claude/skills"
    for skill, content in {
        "bmad-build-auto": '[skill]\nbmod = "bmod-method"\n',
        "bmod-method": '[bmod]\nrequired_skills = [{skill="bmad"}]\n',
        "bmad": '[skill]\nbmod = "bmod-core-tools"\n',
        "bmod-core-tools": '[bmod]\nrequired_skills = ["bmad"]\n',
    }.items():
        folder = tree / skill
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(skill, encoding="utf-8")
        (folder / "bmod.toml").write_text(content, encoding="utf-8")


@pytest.mark.parametrize("requirement", ['"bmad"', '{skill="bmad"}'])
def test_dependency_closure_copies_records_and_hub_and_reports_missing(
    project, tmp_path, requirement
):
    dependencies(project.project)
    (project.project / ".claude/skills/bmod-core-tools/bmod.toml").write_text(
        f"[bmod]\nrequired_skills = [{requirement}]\n", encoding="utf-8"
    )
    required = install.skill_dependency_closure(
        project.project, ".claude/skills", ["bmad-build-auto"]
    )
    assert required == ("bmad-build-auto", "bmod-method", "bmad", "bmod-core-tools")
    staged = tmp_path / "staged"
    skill = project.project / ".claude/skills/bmad-build-auto"
    install._stage_render_copy(
        project.project, skill, staged, staged / ".claude/skills/bmad-build-auto"
    )
    assert all((staged / ".claude/skills" / name / "SKILL.md").is_file() for name in required)
    (project.project / ".claude/skills/bmod-core-tools/SKILL.md").unlink()
    with pytest.raises(ValueError, match="missing declared skill dependency"):
        install.skill_dependency_closure(project.project, ".claude/skills", ["bmad-build-auto"])


def test_fresh_tracked_v7_worker_uses_legacy_bytes_and_retained_worker_is_untouched(
    project, tmp_path
):
    runtime = bundle(tmp_path)
    root = project.project
    install_build_auto_skill(root, folder_id=False)
    tracked_skill = root / ".claude/skills/bmad-build-auto/SKILL.md"
    tracked_skill.write_text("v7 ticket workflow\n", encoding="utf-8")
    script = root / "_bmad/scripts/render_skill.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# v7 renderer\n", encoding="utf-8")
    git(root, "add", "-f", str(tracked_skill.relative_to(root)), str(script.relative_to(root)))
    git(root, "commit", "-m", "Track v7 runtime")
    worker = tmp_path / "worker"
    git(root, "worktree", "add", "-b", "legacy-worker", str(worker))
    pins = {}
    worktree_flow.provision_worktree(
        worker,
        [get_profile("claude")],
        root,
        runtime=runtime,
        fresh=True,
        on_pinned=lambda rel, dialect, text: pins.update({rel: {"dialect": dialect, "text": text}}),
    )
    assert (worker / tracked_skill.relative_to(root)).read_text() == "legacy workflow\n"
    assert (worker / script.relative_to(root)).read_text() == "# preserved renderer\n"
    assert install.missing_stories_support(worker, [".claude/skills"]) == []
    assert worktree_flow._pinned_config_edits(worker, pins) == []
    assert "bmad-build-auto" not in git(worker, "status", "--porcelain")
    assert tracked_skill.read_text() == "v7 ticket workflow\n"
    retained = worker / tracked_skill.relative_to(root)
    retained.write_text("retained edit\n", encoding="utf-8")
    assert (
        worktree_flow.provision_worktree(worker, [get_profile("claude")], root, runtime=runtime)
        == []
    )
    assert (
        worktree_flow.provision_worktree(
            worker, [get_profile("claude")], root, runtime=runtime, fresh=True
        )
        == []
    )
    assert retained.read_text() == "retained edit\n"
    assert any(
        "runtime bytes changed" in item for item in worktree_flow._pinned_config_edits(worker, pins)
    )


def test_preflight_uses_persisted_legacy_source_even_after_policy_switch(
    project, tmp_path, monkeypatch
):
    runtime = bundle(tmp_path)
    pol = dataclasses.replace(
        policy.Policy(),
        stories=policy.StoriesPolicy(source="tickets", legacy_runtime=str(runtime)),
        scm=dataclasses.replace(policy.ScmPolicy(), isolation="worktree"),
    )
    monkeypatch.setattr(cli, "_skill_trees", lambda *args, **kwargs: [".claude/skills"])
    assert cli._require_base_skills(
        project.project, pol, project.repo_root, None, require_stories=True, source="stories"
    )
    (runtime / "_bmad/scripts/render_skill.py").write_text("changed", encoding="utf-8")
    assert not cli._require_base_skills(
        project.project, pol, project.repo_root, None, require_stories=True, source="stories"
    )


def test_v7_project_cannot_fall_back_to_v7_for_legacy_queue(project):
    marker = project.project / ".agents/skills/bmad-build-auto/step-01-clarify-and-route.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("method/scripts/tickets.py", encoding="utf-8")
    with pytest.raises(legacy_runtime.RuntimeError, match="no v7 fallback"):
        legacy_runtime.selected(project.project, policy.Policy(), "stories")
    assert legacy_runtime.selected(project.project, policy.Policy(), "tickets") is None


def test_runtime_guard_wraps_session_before_and_after_and_stops_tampering(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bmad_loop.engine import Engine
    from bmad_loop.model import StoryTask

    path = tmp_path / "runtime.py"
    path.write_text("verified", encoding="utf-8")
    task = StoryTask("1-1-runtime", 1)
    task.pinned_config_rewrites = {"runtime.py": {"dialect": "legacy-runtime", "text": "verified"}}

    class Paused(Exception):
        pass

    def pause(*args, **kwargs):
        raise Paused(str(args))

    flow = SimpleNamespace(_pause=pause)
    flow.guard_legacy_runtime = lambda task, root: worktree_flow.WorktreeFlow.guard_legacy_runtime(
        flow, task, root
    )
    engine = object.__new__(Engine)
    engine._worktree_flow = flow
    engine.workspace = SimpleNamespace(root=tmp_path)
    calls = []

    def session(*args, **kwargs):
        calls.append("session")
        path.write_text("tampered", encoding="utf-8")

    monkeypatch.setattr(engine, "_run_session_impl", session)
    with pytest.raises(Paused, match="runtime bytes changed"):
        engine._run_session(task, "dev", "test", 1)
    assert calls == ["session"]
    with pytest.raises(Paused, match="runtime bytes changed"):
        engine._run_session(task, "dev", "test", 1)
    assert calls == ["session"]
    with pytest.raises(Paused, match="runtime bytes changed"):
        worktree_flow.WorktreeFlow.merge_local(flow, task, SimpleNamespace(path=tmp_path))


def test_v7_worker_carries_dependency_closure_and_detects_missing_record(project, tmp_path):
    install_build_auto_skill(project.project)
    dependencies(project.project)
    worker = tmp_path / "ticket-worker"
    worker.mkdir()
    worktree_flow.provision_worktree(worker, [get_profile("claude")], project.project)
    assert (worker / ".claude/skills/bmod-method/bmod.toml").is_file()
    assert (worker / ".claude/skills/bmad/bmod.toml").is_file()
    assert (worker / ".claude/skills/bmod-core-tools/bmod.toml").is_file()
    assert (
        worktree_flow.base_skills_seed_incomplete(worker, project.project, [".claude/skills"]) == []
    )
    (worker / ".claude/skills/bmod-core-tools/bmod.toml").unlink()
    assert ".claude/skills/bmod-core-tools/bmod.toml" in worktree_flow.base_skills_seed_incomplete(
        worker, project.project, [".claude/skills"]
    )
    (worker / ".claude/skills/bmod-core-tools/SKILL.md").unlink()
    assert ".claude/skills/bmod-core-tools" in worktree_flow.base_skills_seed_incomplete(
        worker, project.project, [".claude/skills"]
    )


def test_content_addressed_bundle_rejects_manifest_replacement(tmp_path):
    runtime = bundle(tmp_path)
    digest = hashlib.sha256((runtime / "manifest.json").read_bytes()).hexdigest()
    pinned = runtime.rename(tmp_path / ("legacy-" + digest[:16]))
    legacy_runtime.verify(pinned)
    (pinned / "_bmad/scripts/render_skill.py").write_text("replacement", encoding="utf-8")
    seal(pinned)
    with pytest.raises(legacy_runtime.RuntimeError, match="content-addressed location"):
        legacy_runtime.verify(pinned)


def test_render_snapshot_symlink_cannot_prove_a_fresh_workflow(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "workflow.md").write_text("unchanged external workflow\n", encoding="utf-8")
    body = f"(root / '_bmad/render').symlink_to({str(external)!r}, target_is_directory=True)\nsys.stdout.write('unchanged external workflow\\n')\n"
    install_render_probe_fixture(root, tmp_path / "marker", body)
    [finding] = install.dev_renderer_probe(root, [".claude/skills"])
    assert finding.severity == "problem"


@pytest.mark.parametrize("requirement", ["1", '{version="1"}', '"../escape"', '""'])
def test_dependency_closure_rejects_invalid_requirement_forms(project, requirement):
    dependencies(project.project)
    (project.project / ".claude/skills/bmod-core-tools/bmod.toml").write_text(
        f"[bmod]\nrequired_skills = [{requirement}]\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid (required skill|declared skill dependency)"):
        install.skill_dependency_closure(project.project, ".claude/skills", ["bmad-build-auto"])
