import tempfile
import unittest
from pathlib import Path

from scout.config import ConfigError, CredentialStore, parse_config
from scout.review_plan import build_review_plan


class ConfigTests(unittest.TestCase):
    def test_parse_minimal_config(self):
        config = parse_config(
            {
                "service": {"state_db": "/tmp/scout.db", "state_dir": "/tmp/scout"},
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
            }
        )
        self.assertEqual(config.bitbucket.api_auth, "basic")
        self.assertEqual(config.polling.interval_seconds, 600)
        self.assertEqual(config.queue.max_parallel_reviews, 2)
        self.assertEqual(config.queue.job_timeout_seconds, 1800)
        self.assertEqual(config.queue.retry_backoff_seconds, 300)
        self.assertEqual(config.service.retention_days, 7)
        self.assertEqual(config.agents.strategy, "codex")
        self.assertEqual(config.agents.providers, ["codex"])
        self.assertEqual(config.agents.codex.model, "gpt-5.5")
        self.assertEqual(config.agents.codex.reasoning_effort, "xhigh")
        self.assertTrue(config.agents.codex.fast_mode)
        self.assertEqual(config.agents.codex.timeout_seconds, 1800)
        self.assertEqual(config.agents.codex.max_subagents, 15)
        self.assertEqual(config.agents.codex.subagent_max_per_lens, 3)
        self.assertEqual(config.agents.codex.subagent_small_loc_limit, 150)
        self.assertEqual(config.agents.codex.subagent_medium_loc_limit, 600)
        self.assertEqual(config.agents.codex.subagent_large_loc_limit, 1500)
        self.assertEqual(config.agents.codex.subagent_high_risk_bonus, 1)
        self.assertFalse(config.agents.claude.enabled)
        self.assertEqual(config.agents.claude.command, "claude")
        self.assertEqual(config.agents.claude.timeout_seconds, 1800)
        self.assertEqual(config.agents.claude.model, "claude-sonnet-4-6")
        self.assertEqual(config.agents.claude.effort, "max")
        self.assertEqual(config.agents.claude.max_subagents, 20)
        self.assertEqual(config.agents.claude.subagent_max_per_lens, 1)
        self.assertEqual(config.agents.claude.subagent_small_loc_limit, 150)
        self.assertEqual(config.agents.claude.subagent_medium_loc_limit, 600)
        self.assertEqual(config.agents.claude.subagent_large_loc_limit, 1500)
        self.assertEqual(config.agents.claude.subagent_high_risk_bonus, 1)
        self.assertEqual(config.reports.report_id, "scout-codex-v1")
        self.assertEqual(config.reports.title, "Codex PR Review")
        self.assertEqual(config.reports.report_id_for("codex"), "scout-codex-v1")
        self.assertEqual(config.reports.title_for("codex"), "Codex PR Review")
        self.assertEqual(config.review.subagent_small_loc_limit, 150)
        self.assertEqual(config.review.subagent_medium_loc_limit, 600)
        self.assertEqual(config.review.subagent_large_loc_limit, 1500)
        self.assertEqual(config.review.subagent_high_risk_bonus, 1)
        self.assertEqual(config.review.subagent_max_per_lens, 4)
        self.assertEqual(config.review.output_mode, "reports")
        self.assertTrue(config.review.risk.enabled)
        self.assertEqual(config.review.risk.provider, "codex")
        self.assertEqual(config.review.risk.timeout_seconds, 120)
        self.assertEqual(config.review.risk.model, "gpt-5.4")
        self.assertEqual(config.review.risk.effort, "low")
        self.assertEqual(config.review.request_comments.provider, "codex")
        self.assertEqual(config.review.request_comments.timeout_seconds, 120)
        self.assertEqual(config.review.request_comments.model, "gpt-5.4")
        self.assertEqual(config.review.request_comments.effort, "low")
        self.assertTrue(config.comments.critical_enabled)
        self.assertEqual(config.comments.severities, ["CRITICAL"])
        self.assertEqual(config.bitbucket.ssh_key_credential, "bitbucket_ssh_key")
        self.assertEqual(config.bitbucket.oauth_client_id_credential, "bitbucket_oauth_client_id")
        self.assertEqual(config.bitbucket.oauth_client_secret_credential, "bitbucket_oauth_client_secret")
        self.assertEqual(config.bitbucket.oauth_token_url, "https://bitbucket.org/site/oauth2/access_token")
        self.assertEqual(config.bitbucket.repositories[0].pr_ids, [])
        self.assertEqual(config.bitbucket.repositories[0].ignored_target_branches, [])
        self.assertFalse(config.bitbucket.repositories[0].ignore_draft_pull_requests)

    def test_parse_oauth_bitbucket_auth(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "api_auth": "oauth_client_credentials",
                    "oauth_client_id_credential": "bb_client_id",
                    "oauth_client_secret_credential": "bb_client_secret",
                    "oauth_token_url": "https://example.test/oauth/token",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
            }
        )
        self.assertEqual(config.bitbucket.api_auth, "oauth_client_credentials")
        self.assertEqual(config.bitbucket.oauth_client_id_credential, "bb_client_id")
        self.assertEqual(config.bitbucket.oauth_client_secret_credential, "bb_client_secret")
        self.assertEqual(config.bitbucket.oauth_token_url, "https://example.test/oauth/token")

    def test_rejects_unknown_bitbucket_auth(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "api_auth": "oauth",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                }
            )

    def test_parse_can_disable_critical_comments(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "comments": {"critical_enabled": False},
            }
        )
        self.assertFalse(config.comments.critical_enabled)
        self.assertEqual(config.comments.severities, [])

    def test_parse_comment_severities(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "comments": {"severities": ["critical", "HIGH", "high", "medium"]},
            }
        )
        self.assertEqual(config.comments.severities, ["CRITICAL", "HIGH", "MEDIUM"])

    def test_rejects_invalid_comment_severity(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "comments": {"severities": ["urgent"]},
                }
            )

    def test_rejects_artifact_retention_above_one_week(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "service": {"retention_days": 8},
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                }
            )

    def test_rejects_claude_strategy_unless_explicitly_enabled(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"strategy": "claude"},
                }
            )

    def test_parse_claude_strategy_defaults_report_identity_when_enabled(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {"strategy": "claude", "claude": {"enabled": True}},
            }
        )
        self.assertEqual(config.agents.strategy, "claude")
        self.assertEqual(config.agents.providers, ["claude"])
        self.assertTrue(config.agents.claude.enabled)
        self.assertEqual(config.agents.claude.auth_mode, "logged_in")
        self.assertEqual(config.agents.claude.credential, "claude")
        self.assertEqual(config.agents.claude.home_dir, "/var/lib/scout/agents/claude/main")
        self.assertEqual(config.agents.claude.max_parallel, 2)
        self.assertEqual(config.agents.claude.timeout_seconds, 1800)
        self.assertEqual(config.agents.claude.command, "claude")
        self.assertEqual(config.agents.claude.model, "claude-sonnet-4-6")
        self.assertEqual(config.agents.claude.effort, "max")
        self.assertEqual(config.agents.claude.max_subagents, 20)
        self.assertEqual(config.agents.claude.subagent_max_per_lens, 1)
        self.assertEqual(config.reports.report_id, "scout-claude-v1")
        self.assertEqual(config.reports.title, "Claude PR Review")
        self.assertEqual(config.review.risk.provider, "codex")
        self.assertEqual(config.review.risk.model, "gpt-5.4")
        self.assertEqual(config.review.risk.effort, "low")

    def test_parse_multi_provider_selection_with_provider_report_defaults(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {"providers": ["codex", "claude"], "claude": {"enabled": True}},
            }
        )
        self.assertEqual(config.agents.strategy, "codex")
        self.assertEqual(config.agents.providers, ["codex", "claude"])
        self.assertEqual(config.reports.report_id_for("codex"), "scout-codex-v1")
        self.assertEqual(config.reports.title_for("codex"), "Codex PR Review")
        self.assertEqual(config.reports.report_id_for("claude"), "scout-claude-v1")
        self.assertEqual(config.reports.title_for("claude"), "Claude PR Review")

    def test_parse_multi_provider_report_overrides(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {"providers": ["codex", "claude"], "claude": {"enabled": True}},
                "reports": {
                    "codex": {"report_id": "custom-codex", "title": "Custom Codex Review"},
                    "claude": {"report_id": "custom-claude", "title": "Custom Claude Review"},
                },
            }
        )
        self.assertEqual(config.reports.report_id_for("codex"), "custom-codex")
        self.assertEqual(config.reports.title_for("codex"), "Custom Codex Review")
        self.assertEqual(config.reports.report_id_for("claude"), "custom-claude")
        self.assertEqual(config.reports.title_for("claude"), "Custom Claude Review")

    def test_rejects_legacy_report_id_for_multi_provider_config(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"providers": ["codex", "claude"], "claude": {"enabled": True}},
                    "reports": {"report_id": "shared-report"},
                }
            )

    def test_rejects_duplicate_multi_provider_report_ids(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"providers": ["codex", "claude"], "claude": {"enabled": True}},
                    "reports": {
                        "codex": {"report_id": "shared-report"},
                        "claude": {"report_id": "shared-report"},
                    },
                }
            )

    def test_parse_claude_effort(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {"claude": {"effort": "medium"}},
            }
        )
        self.assertEqual(config.agents.claude.effort, "medium")

    def test_rejects_unknown_claude_effort(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"claude": {"effort": "extreme"}},
                }
            )

    def test_rejects_unknown_agent_strategy(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"strategy": "gemini"},
                }
            )

    def test_rejects_unknown_agent_provider(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"providers": ["codex", "gemini"]},
                }
            )

    def test_rejects_non_boolean_agent_enabled_flag(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"claude": {"enabled": "false"}},
                }
            )

    def test_rejects_multi_provider_selection_with_default_disabled_claude(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"providers": ["codex", "claude"]},
                }
            )

    def test_parse_repo_pr_id_filter(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [
                        {
                            "slug": "repo",
                            "clone_url": "git@bitbucket.org:ws/repo.git",
                            "pr_ids": [12, 13],
                        }
                    ],
                },
            }
        )
        self.assertEqual(config.bitbucket.repositories[0].pr_ids, [12, 13])

    def test_parse_repo_source_branch_ignore_patterns(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [
                        {
                            "slug": "repo",
                            "clone_url": "git@bitbucket.org:ws/repo.git",
                            "ignored_source_branches": ["^release/", "hotfix/.*"],
                        }
                    ],
                },
            }
        )
        self.assertEqual(
            config.bitbucket.repositories[0].ignored_source_branches,
            ["^release/", "hotfix/.*"],
        )

    def test_parse_repo_target_branch_ignore_patterns(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [
                        {
                            "slug": "repo",
                            "clone_url": "git@bitbucket.org:ws/repo.git",
                            "ignored_target_branches": ["^release/", "^production$"],
                        }
                    ],
                },
            }
        )
        self.assertEqual(
            config.bitbucket.repositories[0].ignored_target_branches,
            ["^release/", "^production$"],
        )

    def test_parse_repo_draft_filter_flag(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [
                        {
                            "slug": "repo",
                            "clone_url": "git@bitbucket.org:ws/repo.git",
                            "ignore_draft_pull_requests": True,
                        }
                    ],
                },
            }
        )
        self.assertTrue(config.bitbucket.repositories[0].ignore_draft_pull_requests)

    def test_rejects_non_boolean_draft_filter_flag(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [
                            {
                                "slug": "repo",
                                "clone_url": "git@bitbucket.org:ws/repo.git",
                                "ignore_draft_pull_requests": "false",
                            }
                        ],
                    },
                }
            )

    def test_rejects_invalid_ignored_source_branch_regex(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [
                            {
                                "slug": "repo",
                                "clone_url": "git@bitbucket.org:ws/repo.git",
                                "ignored_source_branches": ["(unterminated"],
                            }
                        ],
                    },
                }
            )

    def test_rejects_invalid_ignored_target_branch_regex(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [
                            {
                                "slug": "repo",
                                "clone_url": "git@bitbucket.org:ws/repo.git",
                                "ignored_target_branches": ["(unterminated"],
                            }
                        ],
                    },
                }
            )

    def test_parse_optional_bitbucket_ssh_key_credential(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "ssh_key_credential": "deploy_key",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
            }
        )
        self.assertEqual(config.bitbucket.ssh_key_credential, "deploy_key")

    def test_rejects_subagent_plan_above_codex_limit(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "review": {"subagent_max_per_lens": 3},
                    "agents": {"codex": {"max_subagents": 10}},
                }
            )

    def test_rejects_subagent_plan_above_claude_limit(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "review": {"subagent_max_per_lens": 3, "risk": {"provider": "claude"}},
                    "agents": {
                        "strategy": "claude",
                        "codex": {"max_subagents": 20},
                        "claude": {"enabled": True, "max_subagents": 10, "subagent_max_per_lens": 3},
                    },
                }
            )

    def test_provider_default_subagent_lens_caps_are_conservative(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "review": {"subagent_max_per_lens": 4, "risk": {"provider": "claude"}},
                "agents": {
                    "strategy": "claude",
                    "claude": {"enabled": True, "max_subagents": 20},
                },
            }
        )
        self.assertEqual(config.review.subagent_max_per_lens, 4)
        self.assertEqual(config.agents.codex.subagent_max_per_lens, 3)
        self.assertEqual(config.agents.claude.subagent_max_per_lens, 1)

    def test_provider_subagent_lens_overrides_are_parsed(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {
                    "codex": {"subagent_max_per_lens": 2},
                    "claude": {"subagent_max_per_lens": 3},
                },
            }
        )
        self.assertEqual(config.agents.codex.subagent_max_per_lens, 2)
        self.assertEqual(config.agents.claude.subagent_max_per_lens, 3)

    def test_provider_subagent_sizing_overrides_are_parsed_and_applied(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "review": {
                    "subagent_small_loc_limit": 150,
                    "subagent_medium_loc_limit": 600,
                    "subagent_large_loc_limit": 1500,
                    "subagent_high_risk_bonus": 1,
                },
                "agents": {
                    "claude": {
                        "subagent_small_loc_limit": 20,
                        "subagent_medium_loc_limit": 40,
                        "subagent_large_loc_limit": 60,
                        "subagent_high_risk_bonus": 2,
                        "subagent_max_per_lens": 5,
                    },
                },
            }
        )

        self.assertEqual(config.review.subagent_small_loc_limit, 150)
        self.assertEqual(config.agents.claude.subagent_small_loc_limit, 20)
        self.assertEqual(config.agents.claude.subagent_medium_loc_limit, 40)
        self.assertEqual(config.agents.claude.subagent_large_loc_limit, 60)
        self.assertEqual(config.agents.claude.subagent_high_risk_bonus, 2)

        plan = build_review_plan(
            changed_lines=50,
            description="Risk: high",
            small_loc_limit=config.agents.claude.subagent_small_loc_limit,
            medium_loc_limit=config.agents.claude.subagent_medium_loc_limit,
            large_loc_limit=config.agents.claude.subagent_large_loc_limit,
            high_risk_bonus=config.agents.claude.subagent_high_risk_bonus,
            max_subagents_per_lens=config.agents.claude.subagent_max_per_lens,
            risk="high",
        )
        self.assertEqual(plan.subagents_per_lens, 5)

    def test_unselected_provider_subagent_limit_is_not_validated_as_selected_limit(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "review": {"risk": {"provider": "claude"}},
                "agents": {
                    "strategy": "claude",
                    "codex": {"max_subagents": 1},
                    "claude": {"enabled": True, "max_subagents": 20},
                },
            }
        )
        self.assertEqual(config.agents.strategy, "claude")

    def test_parse_risk_classifier_overrides(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {"providers": ["codex", "claude"], "claude": {"enabled": True}},
                "review": {
                    "risk": {
                        "enabled": True,
                        "provider": "claude",
                        "model": "claude-sonnet-4-6",
                        "effort": "high",
                        "timeout_seconds": 45,
                    }
                },
            }
        )
        self.assertTrue(config.review.risk.enabled)
        self.assertEqual(config.review.risk.provider, "claude")
        self.assertEqual(config.review.risk.timeout_seconds, 45)
        self.assertEqual(config.review.risk.model, "claude-sonnet-4-6")
        self.assertEqual(config.review.risk.effort, "high")

    def test_parse_inline_output_mode_and_request_comments_classifier(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {"providers": ["codex", "claude"], "claude": {"enabled": True}},
                "review": {
                    "output_mode": "inline_comments",
                    "request_comments": {
                        "provider": "claude",
                        "model": "claude-sonnet-4-6",
                        "effort": "max",
                        "timeout_seconds": 30,
                    },
                },
                "comments": {"severities": ["critical", "high"]},
            }
        )
        self.assertEqual(config.review.output_mode, "inline_comments")
        self.assertEqual(config.review.request_comments.provider, "claude")
        self.assertEqual(config.review.request_comments.timeout_seconds, 30)
        self.assertEqual(config.review.request_comments.model, "claude-sonnet-4-6")
        self.assertEqual(config.review.request_comments.effort, "max")
        self.assertEqual(config.comments.severities, [])

    def test_rejects_unknown_output_mode(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "review": {"output_mode": "comments"},
                }
            )

    def test_rejects_invalid_request_comments_classifier(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "review": {"request_comments": {"effort": "max"}},
                }
            )

    def test_reports_mode_does_not_require_enabled_request_comment_provider(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {
                    "strategy": "claude",
                    "codex": {"enabled": False},
                    "claude": {"enabled": True},
                },
                "review": {
                    "output_mode": "reports",
                    "risk": {"provider": "claude"},
                },
            }
        )

        self.assertEqual(config.review.output_mode, "reports")
        self.assertEqual(config.review.request_comments.provider, "codex")

    def test_inline_mode_requires_enabled_request_comment_provider(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {
                        "strategy": "claude",
                        "codex": {"enabled": False},
                        "claude": {"enabled": True},
                    },
                    "review": {
                        "output_mode": "inline_comments",
                        "request_comments": {"provider": "codex"},
                        "risk": {"provider": "claude"},
                    },
                }
            )

    def test_inline_mode_ignores_invalid_comment_severities(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "review": {"output_mode": "inline_comments"},
                "comments": {"severities": ["urgent"]},
            }
        )

        self.assertEqual(config.comments.severities, [])

    def test_allows_risk_provider_outside_review_providers(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {"providers": ["claude"], "claude": {"enabled": True}},
                "review": {"risk": {"provider": "codex"}},
            }
        )
        self.assertEqual(config.agents.providers, ["claude"])
        self.assertEqual(config.review.risk.provider, "codex")

    def test_rejects_disabled_risk_provider(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"providers": ["claude"], "codex": {"enabled": False}},
                    "review": {"risk": {"provider": "codex"}},
                }
            )

    def test_rejects_default_disabled_claude_risk_provider(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "review": {"risk": {"provider": "claude"}},
                }
            )

    def test_disabled_risk_allows_unselected_default_provider(self):
        config = parse_config(
            {
                "bitbucket": {
                    "workspace": "ws",
                    "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                },
                "agents": {"strategy": "claude", "claude": {"enabled": True}},
                "review": {"risk": {"enabled": False}},
            }
        )
        self.assertFalse(config.review.risk.enabled)
        self.assertEqual(config.review.risk.provider, "codex")

    def test_rejects_unordered_subagent_loc_limits(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "review": {
                        "subagent_small_loc_limit": 600,
                        "subagent_medium_loc_limit": 150,
                        "subagent_large_loc_limit": 1500,
                    },
                }
            )

    def test_rejects_unordered_provider_subagent_loc_limits(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {
                        "claude": {
                            "subagent_small_loc_limit": 20,
                            "subagent_medium_loc_limit": 10,
                            "subagent_large_loc_limit": 60,
                        },
                    },
                }
            )

    def test_credential_store_reads_systemd_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bitbucket_username"
            path.write_text("alice\n", encoding="utf-8")
            store = CredentialStore(tmp)
            self.assertEqual(store.read("bitbucket_username"), "alice")


if __name__ == "__main__":
    unittest.main()
