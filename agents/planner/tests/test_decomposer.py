"""Tests for the two-level idea decomposer (epics → stories → critic)."""

import json
from unittest.mock import MagicMock, patch

from planner_agent.decomposer import decompose_idea, normalize_plan


def _mock_llm(text: str):
    msg = MagicMock(); msg.content = text
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]
    return resp


def _seq(*payloads):
    """A side_effect list of mock LLM responses, one per expected call."""
    return [_mock_llm(json.dumps(p) if not isinstance(p, str) else p) for p in payloads]


_EPICS = {"project_name": "Auth Module", "epics": [
    {"name": "Password hashing", "description": "Securely hash + verify passwords."},
    {"name": "Tokens", "description": "Issue and validate JWTs."},
]}
_STORIES_A = {"stories": [{"title": "Hash on signup", "description": "repo: dev/app\nHash.", "priority": "high"}]}
_STORIES_B = {"stories": [{"title": "Issue JWT", "description": "repo: dev/app\nJWT.", "priority": "medium"}]}
_NO_MISSING = {"missing": []}


class TestTwoLevelPlan:
    def test_epics_then_stories_flattened_and_tagged(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)):
            plan = decompose_idea("Auth", "Build auth", model="m")
        assert plan["project_name"] == "Auth Module"
        assert [e["name"] for e in plan["epics"]] == ["Password hashing", "Tokens"]
        # stories are flattened epic-by-epic and tagged with their epic
        assert [s["title"] for s in plan["stories"]] == ["Hash on signup", "Issue JWT"]
        assert plan["stories"][0]["epic"] == "Password hashing"
        assert plan["stories"][1]["epic"] == "Tokens"

    def test_completeness_critic_adds_missing_stories(self):
        crit = {"missing": [{"epic": "Tokens", "title": "Refresh tokens",
                             "description": "repo: dev/app\nRotate.", "priority": "high"}]}
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, crit)):
            plan = decompose_idea("Auth", "Build auth", model="m")
        titles = [s["title"] for s in plan["stories"]]
        assert "Refresh tokens" in titles
        # appended under its epic
        assert next(s for s in plan["stories"] if s["title"] == "Refresh tokens")["epic"] == "Tokens"

    def test_critic_can_introduce_a_new_epic(self):
        crit = {"missing": [{"epic": "Accessibility", "title": "Keyboard nav",
                             "description": "repo: dev/app\nA11y.", "priority": "low"}]}
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, crit)):
            plan = decompose_idea("Auth", "Build auth", model="m")
        assert "Accessibility" in [e["name"] for e in plan["epics"]]

    def test_epics_prompt_orders_foundational_first_and_scopes(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)) as mock:
            decompose_idea("Auth", "Build auth", model="m")
        epics_prompt = mock.call_args_list[0][1]["messages"][1]["content"].lower()
        assert "foundational-first" in epics_prompt
        assert "cover the full scope" in epics_prompt

    def test_stories_prompt_forbids_stubs_and_pins_repo(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)) as mock:
            decompose_idea("Auth", "Build auth", model="m", default_repo="alice/backend")
        stories_prompt = mock.call_args_list[1][1]["messages"][1]["content"].lower()
        assert "already scaffolded" in stories_prompt
        assert "signature" in stories_prompt
        assert "alice/backend" in stories_prompt

    def test_repo_prefix_enforced_on_stories(self):
        no_prefix = {"stories": [{"title": "X", "description": "do the thing", "priority": "low"}]}
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, no_prefix, _STORIES_B, _NO_MISSING)):
            plan = decompose_idea("Auth", "Build auth", model="m", default_repo="o/r")
        assert plan["stories"][0]["description"].startswith("repo: o/r")

    def test_api_key_passed_through(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)) as mock:
            decompose_idea("T", "D", model="m", api_key="sk-test")
        assert mock.call_args_list[0][1]["api_key"] == "sk-test"

    def test_locked_decisions_injected_into_prompts(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)) as mock:
            decompose_idea("Auth", "Build auth", model="m",
                           decisions="LOCKED: storage=Postgres; auth=JWT")
        for call in mock.call_args_list:                 # rides on every planning prompt
            assert "storage=Postgres" in call[1]["messages"][1]["content"]

    def test_fallback_when_epics_pass_fails(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq("{bad json")):
            plan = decompose_idea("My Feature", "D", model="m", default_repo="devadmin/sandbox")
        assert plan["module_name"].startswith("My Feature")
        assert len(plan["stories"]) == 1
        assert plan["stories"][0]["description"].startswith("repo: devadmin/sandbox")


class TestNormalizePlan:
    # Pass 1 returns epic names only (no story titles); pass 2 discovers stories per epic.
    _EPICS = {"project_name": "Task API", "epics": [
        {"name": "Data Model", "description": "models + storage"},
        {"name": "Endpoints", "description": "REST"}]}
    _STORIES_1 = {"stories": [{"title": "Task model", "description": "repo: o/r\nmodel", "priority": "high"}]}
    _STORIES_2 = {"stories": [{"title": "CRUD endpoints", "description": "endpoints", "priority": "medium"}]}

    def test_two_pass_maps_plan_to_epic_story_model(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, self._STORIES_1, self._STORIES_2, _NO_MISSING)):
            plan = normalize_plan("## Epic 1 ...\n### Story ...", model="m", default_repo="o/r")
        assert plan["project_name"] == "Task API"
        assert [e["name"] for e in plan["epics"]] == ["Data Model", "Endpoints"]
        assert [s["title"] for s in plan["stories"]] == ["Task model", "CRUD endpoints"]
        assert plan["stories"][0]["epic"] == "Data Model"
        # repo prefix enforced even when the stories pass omits it
        assert plan["stories"][1]["description"].startswith("repo: o/r")

    def test_one_stories_call_per_epic(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, self._STORIES_1, self._STORIES_2, _NO_MISSING)) as mock:
            normalize_plan("plan", model="m", default_repo="o/r")
        assert mock.call_count == 4          # epics + 2 stories + deployability critic

    def test_pass1_epics_only_pass2_discovers_stories(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, self._STORIES_1, self._STORIES_2, _NO_MISSING)) as mock:
            normalize_plan("some plan text", model="m", default_repo="o/r")
        epics_prompt = mock.call_args_list[0][1]["messages"][1]["content"].lower()
        assert "re-structuring an existing plan" in epics_prompt
        assert "do not list individual" in epics_prompt        # pass 1 stays tiny
        assert mock.call_args_list[0][1]["max_tokens"] == 2000
        stories_prompt = mock.call_args_list[1][1]["messages"][1]["content"].lower()
        assert "already scaffolded" in stories_prompt
        assert "find every story" in stories_prompt             # pass 2 self-discovers

    def test_one_bad_epic_does_not_sink_the_plan(self):
        # If an epic's stories pass fails (both attempts), its stories are skipped
        # but the other epics survive.
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, "{bad json", "{bad json", self._STORIES_2, _NO_MISSING)), \
             patch("planner_agent.decomposer.time.sleep"):
            plan = normalize_plan("plan", model="m", default_repo="o/r")
        assert [s["title"] for s in plan["stories"]] == ["CRUD endpoints"]
        assert plan["stories"][0]["epic"] == "Endpoints"

    def test_pass2_sends_only_the_epics_section(self):
        # The cost fix: with matching headings, each pass-2 call carries just that
        # epic's slice of the plan — not the whole document.
        plan_text = ("# Overview\nintro\n"
                     "## Epic 1 — Data Model\nSECTION_A spec details\n"
                     "## Epic 2 — Endpoints\nSECTION_B spec details\n")
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, self._STORIES_1, self._STORIES_2, _NO_MISSING)) as mock:
            normalize_plan(plan_text, model="m", default_repo="o/r")
        p1 = mock.call_args_list[1][1]["messages"][1]["content"]
        p2 = mock.call_args_list[2][1]["messages"][1]["content"]
        assert "SECTION_A" in p1 and "SECTION_B" not in p1
        assert "SECTION_B" in p2 and "SECTION_A" not in p2
        # import stories calls get the long-output budget + timeout + 2 attempts
        assert mock.call_args_list[1][1]["max_tokens"] == 16000
        assert mock.call_args_list[1][1]["timeout"] == 300.0

    def test_unmatched_epic_falls_back_to_full_plan(self):
        plan_text = "no headings at all, just prose describing the work"
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, self._STORIES_1, self._STORIES_2, _NO_MISSING)) as mock:
            normalize_plan(plan_text, model="m", default_repo="o/r")
        assert plan_text in mock.call_args_list[1][1]["messages"][1]["content"]

    def test_import_aborts_after_consecutive_epic_failures(self):
        # Spend guard: 4 epics, every stories call fails → abort after 3 failed
        # epics (2 attempts each) instead of grinding through the 4th.
        four = {"project_name": "P", "epics": [
            {"name": f"E{i}", "description": "d"} for i in range(4)]}
        calls = {"n": 0}
        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _mock_llm(json.dumps(four))
            return _mock_llm("{bad json")
        with patch("planner_agent.decomposer.litellm.completion", side_effect=flaky), \
             patch("planner_agent.decomposer.time.sleep"):
            plan = normalize_plan("plan", model="m", default_repo="o/r")
        assert calls["n"] == 1 + 3 * 2      # epics pass + 3 epics × 2 attempts, 4th skipped
        assert len(plan["stories"]) == 1     # fell back (nothing normalized)

    def test_fallback_when_epics_pass_fails(self):
        with patch("planner_agent.decomposer.litellm.completion", side_effect=_seq("{bad")):
            plan = normalize_plan("x" * 100, model="m", default_repo="devadmin/sandbox")
        assert len(plan["stories"]) == 1
        assert plan["stories"][0]["description"].startswith("repo: devadmin/sandbox")

    def test_deployability_critic_appends_finalization_story(self):
        # After mapping, the import critic can add a foundational story (e.g. declare
        # dependencies) the external spec assumed — tagged to a new epic if needed.
        crit = {"missing": [{"epic": "Project Finalization",
                             "title": "Declare all runtime dependencies + clean-install check",
                             "description": "repo: o/r\nAdd mcp etc. to pyproject; verify pip install .",
                             "priority": "high"}]}
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, self._STORIES_1, self._STORIES_2, crit)):
            plan = normalize_plan("plan", model="m", default_repo="o/r")
        titles = [s["title"] for s in plan["stories"]]
        assert "Declare all runtime dependencies + clean-install check" in titles
        assert "Project Finalization" in [e["name"] for e in plan["epics"]]

    def test_critic_skipped_on_resume(self):
        # Resume fills known-missing epics; it must NOT run the critic (no extra call).
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, self._STORIES_2)) as mock:
            normalize_plan("plan", model="m", default_repo="o/r", skip_epics={"Data Model"})
        assert mock.call_count == 2   # epics + 1 stories pass, no critic

    def test_resume_skips_covered_epics(self):
        # Resume: pass 1 returns both epics; the already-covered one is skipped, so
        # only the missing epic's stories pass runs. Full epic list still returned.
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, self._STORIES_2)) as mock:
            plan = normalize_plan("plan", model="m", default_repo="o/r",
                                  skip_epics={"Data Model"})
        assert mock.call_count == 2                       # epics pass + 1 stories pass (not 2)
        assert [e["name"] for e in plan["epics"]] == ["Data Model", "Endpoints"]
        assert [s["title"] for s in plan["stories"]] == ["CRUD endpoints"]

    def test_resume_returns_empty_not_fallback_when_all_fail(self):
        # On resume, all remaining epics failing yields empty stories (caller: "nothing
        # new this round"), NOT a bogus single-story fallback.
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._EPICS, "{bad", "{bad")), \
             patch("planner_agent.decomposer.time.sleep"):
            plan = normalize_plan("plan", model="m", default_repo="o/r",
                                  skip_epics={"Data Model"})
        assert plan["stories"] == []

    def test_reuse_epics_skips_pass1(self):
        # Supplying the epic list (resume) skips pass 1 entirely — the only calls are the
        # per-epic stories passes — so epic identity stays stable across runs.
        supplied = [{"name": "Data Model", "description": "d"},
                    {"name": "Endpoints", "description": "d"}]
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._STORIES_2)) as mock:
            plan = normalize_plan("plan", model="m", default_repo="o/r",
                                  epics=supplied, skip_epics={"Data Model"})
        assert mock.call_count == 1                       # no pass-1 call; 1 stories pass
        assert [e["name"] for e in plan["epics"]] == ["Data Model", "Endpoints"]
        assert [s["title"] for s in plan["stories"]] == ["CRUD endpoints"]

    def test_rate_limit_uses_longer_backoff(self):
        from planner_agent import decomposer
        good = _mock_llm('{"ok": 1}')
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=[Exception("api_error_status:429 too many requests"), good]), \
             patch("planner_agent.decomposer.time.sleep") as slept:
            decomposer._complete_json([{"role": "user", "content": "x"}],
                                      model="m", api_key="", stack="", redis_conn=None)
        slept.assert_called_once_with(decomposer._RATE_LIMIT_BACKOFF)


class TestClaudeCodeRouting:
    def test_claude_code_model_bypasses_litellm(self):
        # A claude-code planner model must route through the subscription CLI adapter
        # and never touch litellm (no API key, no per-token billing).
        seq = ['{"project_name":"P","epics":[{"name":"E","description":"d"}]}',
               '{"stories":[{"title":"s","description":"repo: o/r\\nx","priority":"low"}]}',
               '{"missing":[]}']
        with patch("planner_agent.claude_code.complete", side_effect=seq) as cli, \
             patch("planner_agent.decomposer.litellm.completion") as llm:
            plan = decompose_idea("T", "D", model="claude-code/opus", default_repo="o/r")
        assert llm.call_count == 0
        assert cli.call_count == 3
        assert cli.call_args.kwargs["model"] == "claude-code/opus"
        assert [s["title"] for s in plan["stories"]] == ["s"]

    def test_cli_failure_retries_then_falls_back(self):
        with patch("planner_agent.claude_code.complete",
                   side_effect=RuntimeError("not logged in")) as cli, \
             patch("planner_agent.decomposer.litellm.completion") as llm, \
             patch("planner_agent.decomposer.time.sleep"):
            plan = decompose_idea("My Feature", "D", model="claude-code",
                                  default_repo="devadmin/sandbox")
        assert llm.call_count == 0
        assert cli.call_count == 3                       # epics pass retried to budget
        assert len(plan["stories"]) == 1                 # graceful single-story fallback


class TestCallHardening:
    def test_complete_json_sets_timeout_and_disables_internal_retry(self):
        from planner_agent.decomposer import _complete_json
        with patch("planner_agent.decomposer.litellm.completion",
                   return_value=_mock_llm('{"ok": 1}')) as mock:
            out = _complete_json([{"role": "user", "content": "x"}],
                                 model="m", api_key="", stack="", redis_conn=None)
        assert out == {"ok": 1}
        assert mock.call_args[1]["timeout"] == 90.0
        assert mock.call_args[1]["num_retries"] == 0

    def test_complete_json_retries_transient_then_succeeds(self):
        from planner_agent.decomposer import _complete_json
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=[Exception("provider error"), _mock_llm('{"ok": 1}')]) as mock, \
             patch("planner_agent.decomposer.time.sleep"):
            out = _complete_json([{"role": "user", "content": "x"}],
                                 model="m", api_key="", stack="", redis_conn=None)
        assert out == {"ok": 1} and mock.call_count == 2

    def test_complete_json_retries_empty_content(self):
        from planner_agent.decomposer import _complete_json
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=[_mock_llm(""), _mock_llm('{"ok": 1}')]) as mock, \
             patch("planner_agent.decomposer.time.sleep"):
            out = _complete_json([{"role": "user", "content": "x"}],
                                 model="m", api_key="", stack="", redis_conn=None)
        assert out == {"ok": 1} and mock.call_count == 2

    def test_complete_json_raises_after_max_attempts(self):
        import pytest
        from planner_agent.decomposer import _complete_json
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=Exception("down")) as mock, \
             patch("planner_agent.decomposer.time.sleep"):
            with pytest.raises(Exception):
                _complete_json([{"role": "user", "content": "x"}],
                               model="m", api_key="", stack="", redis_conn=None)
        assert mock.call_count == 3   # initial + 2 retries
