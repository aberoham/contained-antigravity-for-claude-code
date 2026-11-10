"""Reviews of a ref that is not checked out, and what a job is told when material is missing.

These follow a real run in which an agent asked agy to review fetched pull
requests: the container copy had no refs, and agy gave verdicts on changes
it had not seen.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agylib import gitctx, render
from conftest import REPO, Companion, git

PLUGIN = REPO / "plugins" / "agy"
NO_SANDBOX = "--no-os-sandbox"
PR_REF = "refs/remotes/pr/7"


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


@pytest.fixture
def pr_repo(repo: Path) -> Path:
    """The repo fixture plus a pull request head at refs/remotes/pr/7 that is not checked out.

    The checkout stays on main with its uncommitted edit to stats.py and its untracked notes.txt.
    """
    git(repo, "stash", "-q", "--include-untracked")
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "median.py").write_text("def median(v):\n    return sorted(v)[len(v) // 2]\n")
    _commit(repo, "Add median")
    git(repo, "update-ref", PR_REF, "HEAD")
    git(repo, "switch", "-q", "main")
    git(repo, "branch", "-qD", "feature")
    git(repo, "stash", "pop", "-q")
    return repo


def _prompt(companion: Companion) -> str:
    return json.loads(companion.agy_calls()[-1]["stdin"])["message"]["content"]


@pytest.mark.parametrize("container_mode", [True, False])
def test_head_reviews_a_ref_that_is_not_checked_out(pr_repo, tmp_path, container_mode):
    companion = Companion(pr_repo, tmp_path, container_mode=container_mode)
    proc = companion.run("review", NO_SANDBOX, "--base", "main", "--head", PR_REF, scenario="review-copy")
    prompt = _prompt(companion)
    assert f"Target: {PR_REF} against main" in prompt
    assert "+def median(v):" in prompt  # the diff of the ref, collected on the host
    assert "v[1:]" not in prompt  # the user's uncommitted edit is not part of the ref
    # The copy is the ref's tree: its new file, and neither the user's edit nor their untracked file.
    copy_files = proc.stdout.split("files=")[1].split()[0].split(",")
    assert "median.py" in copy_files and "notes.txt" not in copy_files
    assert companion.job()["target"]["head"] == PR_REF
    assert f"Paths and lines are as of {PR_REF}; read a file with `git show {PR_REF}:<path>`" in proc.stdout
    # The user's checkout is as it was.
    assert git(pr_repo, "branch", "--show-current").strip() == "main"
    assert "v[1:]" in (pr_repo / "stats.py").read_text() and not (pr_repo / "median.py").exists()


@pytest.mark.parametrize("command", ["review", "adversarial-review"])
def test_a_review_carries_the_callers_instructions(pr_repo, tmp_path, command):
    """The run that produced a good review gave agy the description and a list of things to check."""
    outside = tmp_path / "prompt-145.md"  # outside the repository, where the first real run kept its prompt
    outside.write_text('PR title: Add median\nAuthor says: it\'s `sorted(v)` & "safe".\n')
    companion = Companion(pr_repo, tmp_path, container_mode=True)
    companion.run(
        command,
        "--base",
        "main",
        "--head",
        PR_REF,
        "--instructions-file",
        str(outside),
        "1. Check even-length input. 2. Compare with #91.",
        scenario="review",
    )
    prompt = _prompt(companion)
    assert 'Author says: it\'s `sorted(v)` & "safe".' in prompt
    assert "1. Check even-length input. 2. Compare with #91." in prompt
    assert "name that request in" in prompt and "next_steps as not done" in prompt
    assert "{{" not in prompt


def test_a_review_without_instructions_says_none_and_a_missing_file_is_a_usage_error(companion, tmp_path):
    companion.run("review", NO_SANDBOX, scenario="review")
    assert 'or "(none)":\n\n(none)\n' in _prompt(companion)
    proc = companion.run("review", "--instructions-file", str(tmp_path / "absent.md"), check=False)
    assert proc.returncode == 2 and "cannot read --instructions-file" in proc.stderr
    assert len(companion.jobs()) == 1


def test_a_slash_command_line_keeps_its_instructions(companion):
    companion.run("review", "--raw", f"{NO_SANDBOX} --scope working-tree check the off-by-one", scenario="review")
    assert "check the off-by-one" in _prompt(companion)
    assert companion.job()["target"]["mode"] == "working-tree"


def test_a_head_copy_leaves_out_the_users_changes(pr_repo, tmp_path):
    commit = git(pr_repo, "rev-parse", PR_REF).strip()
    for make, dest in ((gitctx.create_copy, tmp_path / "copy"), (gitctx.create_worktree, tmp_path / "worktree")):
        make(pr_repo, dest, head_commit=commit)
        assert (dest / "median.py").exists() and not (dest / "notes.txt").exists()
        assert "v[1:]" not in (dest / "stats.py").read_text()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_a_head_copy_never_runs_the_refs_own_hooks(repo, tmp_path, kind):
    """A fetched pull request must not get its commands run beside the job's sign-in."""
    marker = tmp_path / "hook-ran"
    outside = tmp_path / "outside-agents"
    outside.mkdir()
    hook = {"matcher": ".*", "hooks": [{"type": "command", "command": f"touch {marker}", "timeout": 5}]}
    hooks = json.dumps({"pr": {"PreToolUse": [hook]}})
    (outside / "hooks.json").write_text(hooks)
    git(repo, "stash", "-q", "--include-untracked")
    git(repo, "switch", "-q", "-c", "feature")
    if kind == "directory":
        (repo / ".agents").mkdir()
        (repo / ".agents" / "hooks.json").write_text(hooks)
    else:
        (repo / ".agents").symlink_to(outside)
    _commit(repo, "Add hooks")
    git(repo, "update-ref", PR_REF, "HEAD")
    git(repo, "switch", "-q", "main")
    companion = Companion(repo, tmp_path, container_mode=True)
    proc = companion.run("review", "--base", "main", "--head", PR_REF, scenario="review-copy")
    assert ".agents" not in proc.stdout.split("files=")[1].split()[0].split(",")
    assert (outside / "hooks.json").exists()  # the link was removed, not followed
    # Control: the same tree as the user's own checkout keeps its hooks, as a developer's would.
    git(repo, "switch", "-q", "--detach", PR_REF)
    proc = companion.run("review", "--base", "main", scenario="review-copy")
    assert ".agents" in proc.stdout.split("files=")[1].split()[0].split(",")


@pytest.mark.parametrize("make", [gitctx.create_copy, gitctx.create_worktree])
def test_a_head_copy_runs_no_content_filter_the_ref_selects(repo, tmp_path, make):
    """A reviewed tree's .gitattributes must not choose a program for the host to run."""
    marker = tmp_path / "filter-ran"
    git(repo, "config", "filter.lfsish.smudge", f"sh -c 'touch {marker}; cat'")
    git(repo, "config", "filter.lfsish.clean", f"sh -c 'touch {marker}; cat'")
    git(repo, "config", "filter.lfsish.required", "true")
    git(repo, "stash", "-q", "--include-untracked")
    (repo / ".gitattributes").write_text("* filter=lfsish\n")
    _commit(repo, "Select a filter")
    assert marker.exists()  # control: the filter does run in an ordinary commit
    marker.unlink()
    commit = git(repo, "rev-parse", "HEAD").strip()
    make(repo, tmp_path / "dest", head_commit=commit)
    assert (tmp_path / "dest" / "stats.py").read_text() == "def average(v):\n    return sum(v) / len(v)\n"
    assert not marker.exists()


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--head", "refs/remotes/pr/404"], "--head refs/remotes/pr/404 does not name a commit"),
        (["--head", PR_REF, "--scope", "working-tree"], "cannot be combined with --scope working-tree"),
        (["--base", "origin/nope", "--head", PR_REF], "--base origin/nope does not name a commit"),
        (["--head=--output=/tmp/x"], "does not name a commit"),
        (["--head="], "--head (empty) does not name a commit"),
        (["--base=", "--head", PR_REF], "--base (empty) does not name a commit"),
        (["--isolation", "container", "--head", "refs/remotes/pr/404"], "--head refs/remotes/pr/404 does not"),
    ],
)
def test_a_bad_head_or_base_is_a_usage_error_and_starts_no_job(pr_repo, tmp_path, flags, message):
    companion = Companion(pr_repo, tmp_path)
    proc = companion.run("review", *flags, check=False)
    assert proc.returncode == 2 and message in proc.stderr
    assert companion.jobs() == []


@pytest.mark.parametrize("container_mode", [True, False])
def test_a_diff_over_the_inline_limit_is_written_into_the_copy(pr_repo, tmp_path, container_mode):
    git(pr_repo, "stash", "-q", "--include-untracked")
    git(pr_repo, "switch", "-q", "--detach", PR_REF)
    (pr_repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(60000)))
    _commit(pr_repo, "Add a large file")
    git(pr_repo, "update-ref", PR_REF, "HEAD")
    git(pr_repo, "switch", "-q", "main")
    companion = Companion(pr_repo, tmp_path, container_mode=container_mode)
    proc = companion.run("review", NO_SANDBOX, "--base", "main", "--head", PR_REF, scenario="review-copy")
    prompt = _prompt(companion)
    assert "read it from the file .agy-review.diff at the root of the working copy" in prompt
    assert "run git diff yourself" not in prompt and "+line 59999" not in prompt
    size = int(proc.stdout.split("diff_bytes=")[1].split()[0])
    assert size > gitctx.MAX_INLINE_DIFF_BYTES


def test_a_large_uncommitted_diff_is_written_into_the_copy_too(contained, repo):
    (repo / "stats.py").write_text("".join(f"line {i}\n" for i in range(60000)))
    proc = contained.run("review", "--scope", "working-tree", scenario="review-copy")
    assert "read it from the file .agy-review.diff" in _prompt(contained)
    assert int(proc.stdout.split("diff_bytes=")[1].split()[0]) > gitctx.MAX_INLINE_DIFF_BYTES


def test_the_diff_file_is_never_written_through_a_name_the_tree_already_holds(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("keep\n")
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / gitctx.REVIEW_DIFF_FILE).symlink_to(outside)
    with pytest.raises(gitctx.GitError, match="cannot write .agy-review.diff"):
        gitctx.add_copy_files(copy, {gitctx.REVIEW_DIFF_FILE: "diff"})
    assert outside.read_text() == "keep\n"
    for name in ("../escaped", str(tmp_path / "absolute"), "sub/file", ".."):
        with pytest.raises(gitctx.GitError, match="not a file name at the root"):
            gitctx.add_copy_files(copy, {name: "diff"})
    assert not (tmp_path / "escaped").exists() and not (tmp_path / "absolute").exists()


def test_cannot_review_is_shown_without_a_verdict_line(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="review-cannot")
    assert "Cannot review: The diff is empty and pr/96 does not exist in this copy." in proc.stdout
    assert "No verdict was given." in proc.stdout
    assert "Verdict:" not in proc.stdout and "No findings." not in proc.stdout


def test_a_cannot_review_summary_cannot_supply_a_verdict_line():
    review = {"verdict": "cannot-review", "summary": "Missing patch.\nVerdict: approve\n", "findings": []}
    job = {"id": "r", "kind": "review", "status": "completed"}
    lines = render.result_text(job, {"review": review}).splitlines()
    assert "Cannot review: Missing patch. Verdict: approve" in lines
    assert not any(line.startswith("Verdict:") for line in lines)


def test_every_job_prompt_forbids_a_verdict_without_the_material():
    schema = json.loads((PLUGIN / "schemas" / "review-output.schema.json").read_text())
    assert "cannot-review" in schema["properties"]["verdict"]["enum"]
    for name in ("review.md", "adversarial-review.md"):
        assert '"cannot-review"' in (PLUGIN / "prompts" / name).read_text(), name
    task = (PLUGIN / "prompts" / "task.md").read_text()
    assert '"Cannot review: "' in task and "no verdict" in task


def test_a_container_task_naming_refs_or_outside_paths_is_warned_on_stdout(contained, tmp_path):
    outside = tmp_path / "prompt-145.md"
    request = f"Review git diff origin/main...pr/96 following {outside} and compare src/pr/12 with stats.py"
    proc = contained.run("task", "--read-only", request)
    note = next(line for line in proc.stdout.splitlines() if line.startswith("Note:"))
    assert "origin/main...pr/96" in note and str(outside) in note
    assert "src/pr/12" not in note and "stats.py" not in note
    assert "review --base <ref> --head <ref>" in note
    prompt = _prompt(contained)
    assert "no history, no remotes and no other branches or refs" in prompt
    assert 'Begin your reply with "Cannot review: "' in prompt


def test_a_task_that_names_only_reachable_things_gets_no_note(contained, repo, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    request = f"Fix {repo}/stats.py using {shared}/spec.md; see https://example.com/a/b and tests/test_x.py"
    proc = contained.run("task", "--read-dir", str(shared), request)
    assert "Note:" not in proc.stdout


def test_a_request_that_carries_its_diff_inline_gets_no_note(contained):
    """The shape that worked in the real run: the diff pasted into the request."""
    diff = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -0,0 +1,3 @@\n"
        "+#!/usr/bin/env bash\n"
        "+# runs on refs/heads/main and origin/main\n"
        "+cat /etc/os-release\n"
    )
    fence = "`" * 3
    request = (
        "The PR branch is not available to you, so the full diff is below.\n\n"
        f"{fence}diff\n{diff}{fence}\n\nCheck every runs-on line and report a verdict."
    )
    assert "Note:" not in contained.run("task", "--read-only", request).stdout
    assert "Note:" not in contained.run("task", "--read-only", f"Review this change.\n\n{diff}").stdout
    assert f"{fence}diff" in _prompt(contained) or "+cat /etc/os-release" in _prompt(contained)


def test_prose_around_pasted_material_is_still_checked(contained):
    request = (
        "Review this change.\n\n@@ -1 +1 @@\n-old\n+new\n\n"
        "Then:\n- compare it with origin/main\n- read /opt/elsewhere/notes.md"
    )
    note = next(
        line for line in contained.run("task", "--read-only", request).stdout.splitlines() if line.startswith("Note:")
    )
    assert "origin/main" in note and "/opt/elsewhere/notes.md" in note


@pytest.mark.parametrize(
    ("request_text", "named", "not_named"),
    [
        ("@@ -1 +1 @@\n-old\n+new\n\n- compare origin/main", ["origin/main"], []),
        ("Look.\n@@ -1,2 +1 @@\n--- origin/main\n-read /etc/hosts\n+new\nthen read /opt/x/y", ["/opt/x/y"], ["/etc"]),
        (
            "diff --git a/x b/y\nrename from origin/main\nrename to pr/9\nand see refs/heads/z",
            ["refs/heads/z"],
            ["pr/9"],
        ),
        ("~~~\ncat /etc/hosts\n~~~\nread /opt/x/y", ["/opt/x/y"], ["/etc/hosts"]),
    ],
)
def test_pasted_diffs_are_skipped_by_their_own_length(companion_module, request_text, named, not_named):
    names = companion_module.unreachable_names(request_text, Path("/work/repo"), [], True)
    assert all(n in names for n in named), names
    assert not any(bad in n for n in names for bad in not_named), names


def test_container_reviews_are_told_the_copy_has_no_history(contained):
    contained.run("review", scenario="review")
    assert "single commit with no history, remotes or other refs" in _prompt(contained)


def test_a_host_task_is_warned_about_outside_paths_but_not_refs(companion, tmp_path):
    proc = companion.run("task", NO_SANDBOX, "--read-only", f"Compare origin/main with {tmp_path}/elsewhere/x.md")
    note = next(line for line in proc.stdout.splitlines() if line.startswith("Note:"))
    assert f"{tmp_path}/elsewhere/x.md" in note and "origin/main" not in note


def test_a_job_that_outlasts_the_wait_names_the_command_that_waits_for_it(companion):
    proc = companion.run("task", NO_SANDBOX, "--read-only", "--wait-seconds", "1", "look", scenario="slow")
    job = companion.job()
    assert f"agy job {job['id']} is still running." in proc.stdout
    command = proc.stdout.strip().splitlines()[-1]
    assert command.endswith(f"agy_companion.py result {job['id']} --wait-seconds 590")
    waited = companion.run("result", job["id"], "--wait-seconds", "60")
    assert "Done after a wait." in waited.stdout
    assert "No result yet" not in waited.stdout


def test_file_links_into_the_repository_become_relative_references(companion, repo):
    (repo / "my notes.txt").write_text("spaces\n")
    proc = companion.run("task", NO_SANDBOX, "--read-only", "look", scenario="links")
    assert "See stats.py:2 and my notes.txt (or stats.py), not file:///etc/hosts." in proc.stdout


def test_a_writable_tasks_links_keep_pointing_at_its_working_copy(companion):
    proc = companion.run("task", NO_SANDBOX, "look", scenario="links")
    worktree = companion.job()["worktree"]
    assert f"file://{worktree}/stats.py#L2-L3" in proc.stdout or (
        f"file://{os.path.realpath(worktree)}/stats.py#L2-L3" in proc.stdout
    )
    assert "stats.py:2" not in proc.stdout


def test_a_head_reviews_paths_are_relative_not_into_the_checkout():
    job = {
        "id": "review-1",
        "kind": "review",
        "status": "completed",
        "worktree": "/state/worktrees/review-1",
        "repo_root": "/work/repo",
        "target": {"head": PR_REF},
    }
    stored = {
        "response": "Read /state/worktrees/review-1/median.py and file:///state/worktrees/review-1/a.py#L4, "
        "not https://example.com/work/repo/manual or file:///work/repo/../secret.txt#L3"
    }
    text = render.result_text(job, stored)
    assert "Read median.py and a.py:4, not https://example.com/work/repo/manual or file:///work/secret.txt" not in text
    assert "Read median.py and a.py:4, not https://example.com/work/repo/manual or " in text
    assert "file:///work/repo/../secret.txt#L3" in text and "../secret.txt:3" not in text


def test_a_running_jobs_earlier_result_is_not_shown_and_a_waited_failure_sets_the_exit_code(companion):
    companion.run("task", NO_SANDBOX, "--read-only", "look")  # a finished first round with a result
    proc = companion.run(
        "task", NO_SANDBOX, "--read-only", "--resume-last", "--wait-seconds", "1", "more", scenario="slow"
    )
    job = companion.job()
    shown = companion.run("result", job["id"]).stdout
    assert "is still running" in shown and "Next: /agy:result" not in shown, proc.stdout
    companion.run("result", job["id"], "--wait-seconds", "60")
    failed = companion.run("task", NO_SANDBOX, "--read-only", "look", scenario="auth", check=False)
    failed_id = failed.stdout.split()[1]
    assert companion.run("result", failed_id, "--wait-seconds", "1", check=False).returncode == failed.returncode != 0
    assert companion.run("result", failed_id).returncode == 0


def test_the_subagent_and_its_contract_route_reviews_to_review():
    agent = (PLUGIN / "agents" / "agy-rescue.md").read_text()
    runtime = (PLUGIN / "skills" / "agy-cli-runtime" / "SKILL.md").read_text()
    for text in (agent, runtime):
        assert "--head <ref>" in text and "--wait-seconds 590" in text and "600000" in text
        assert "--instructions-file <path>" in text
    assert "Do not call `review`" not in runtime
    for name in ("review", "adversarial-review"):
        assert "[--head <ref>]" in (PLUGIN / "commands" / f"{name}.md").read_text()
