import json
import unittest
from dataclasses import replace

from scout.deduplication import (
    SELECTION_SCHEMA, SelectionFinding, SelectionValidationError, exact_selection, extract_selection, prepare_selection,
)


def finding(id, body="Socket leaked on timeout. Close it before returning.", severity="HIGH", **kwargs):
    annotation = dict(path="client.py", line=12, line_side="NEW", severity=severity,
                      reviewer="correctness", external_id=id)
    annotation.update(kwargs)
    return SelectionFinding(id, annotation, body, "source", "base", "codex")


def decision(id, action="retain", target=None, relationship=None):
    return dict(candidate_id=id, decision=action, covered_by=target,
                relationship=relationship, reason="Direct failing-condition coverage.")


def output(*decisions, supersessions=None):
    return json.dumps(dict(decisions=decisions, historical_supersessions=supersessions or []))


class SelectionTests(unittest.TestCase):
    def test_exact_ignores_provenance_and_uses_highest_severity(self):
        first = finding("a", severity="MEDIUM")
        second = replace(finding("b", severity="CRITICAL"), provider="claude")
        plan = exact_selection([first, second])
        self.assertEqual(plan.retained_ids, ["b"])
        self.assertEqual(plan.severities, {"b": "CRITICAL"})
        self.assertEqual(plan, exact_selection([second, first]))

    def test_schema_asks_for_reason_before_decision(self):
        item = SELECTION_SCHEMA["properties"]["decisions"]["items"]
        for order in (list(item["properties"]), item["required"]):
            self.assertLess(order.index("reason"), order.index("decision"))

    def test_disabled_retains_exact_duplicates(self):
        self.assertEqual(exact_selection([finding("a"), finding("b")], enabled=False).retained_ids, ["a", "b"])

    def test_exact_requires_revision_location_and_published_content(self):
        original = finding("a")
        different = [replace(finding("b"), source_commit="other"), finding("c", line=13),
                     finding("d", body="Different issue"), finding("e", line_side="OLD")]
        self.assertEqual(len(exact_selection([original] + different).retained_ids), 5)

    def test_history_only_covers_same_or_lower_severity_and_known_revision(self):
        candidate = finding("a")
        for historical in (finding("h", severity="MEDIUM"), replace(finding("h"), source_commit="")):
            self.assertEqual(exact_selection([candidate], [historical]).retained_ids, ["a"])
        self.assertEqual(exact_selection([candidate], [finding("h")]).retained_ids, [])

    def test_exact_higher_severity_root_records_historical_successor(self):
        plan = exact_selection([finding("a")], [finding("h", severity="LOW")])
        self.assertEqual(plan.superseded_history, {"a": ["h"]})
        self.assertEqual(exact_selection([finding("a")], [finding("h", severity="LOW")], enabled=False).superseded_history, {})

    def test_broader_original_wins_and_inherits_severity(self):
        narrow = finding("a", severity="CRITICAL")
        broad = finding("b", body="Leaks on timeout and authentication failure; close on both paths.", severity="MEDIUM")
        payload = output(decision("a", "covered", "b", "representative_subsumes_candidate"), decision("b"))
        for candidates in ([narrow, broad], [broad, narrow]):
            plan = extract_selection(payload, prepare_selection(candidates))
            self.assertEqual(plan.retained_ids, ["b"])
            self.assertEqual(plan.severities, {"b": "CRITICAL"})
        self.assertEqual(broad.annotation["severity"], "MEDIUM")

    def test_partial_overlap_uncertain_and_distinct_issues_survive(self):
        plan = extract_selection(output(decision("a"), decision("b", "uncertain")),
                                 prepare_selection([finding("a"), finding("b", body="Missing auth")]))
        self.assertEqual(plan.retained_ids, ["a", "b"])

    def test_sensitive_cross_path_suppression_becomes_uncertain(self):
        for annotation in (dict(severity="CRITICAL"), dict(reviewer="security")):
            plan = extract_selection(output(decision("a", "covered", "b", "equivalent"), decision("b")),
                                     prepare_selection([finding("a", **annotation), finding("b", path="other.py")]))
            self.assertEqual(plan.retained_ids, ["a", "b"])
            self.assertEqual(plan.decisions[0]["decision"], "uncertain")

    def test_rejects_cycles_chains_missing_and_invented_ids(self):
        inputs = prepare_selection([finding("a"), finding("b"), finding("c")])
        invalid = [
            output(decision("a"), decision("b")),
            output(decision("a"), decision("b"), decision("x")),
            output(decision("a", "covered", "b", "equivalent"), decision("b", "covered", "a", "equivalent"), decision("c")),
            output(decision("a", "covered", "b", "equivalent"), decision("b", "covered", "c", "equivalent"), decision("c")),
            output(decision("a", "covered", "a", "equivalent"), decision("b"), decision("c")),
            output(decision("a", "covered", "c", "equivalent"), decision("b"), decision("c", "uncertain")),
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(SelectionValidationError):
                extract_selection(raw, inputs)

    def test_rejects_lower_severity_history(self):
        with self.assertRaises(SelectionValidationError):
            extract_selection(output(decision("a", "covered", "h", "equivalent")),
                              prepare_selection([finding("a")], [finding("h", severity="LOW")]))

    def test_count_cap_preserves_omitted_candidates_and_direct_subset_match(self):
        inputs = prepare_selection([finding("c"), finding("b"), finding("a")], max_input_findings=2)
        plan = extract_selection(output(decision("a", "covered", "b", "equivalent"), decision("b")), inputs)
        self.assertFalse(plan.comparison_complete)
        self.assertEqual(plan.retained_ids, ["b", "c"])
        self.assertEqual(plan.decisions[-1]["decision"], "uncertain")

    def test_utf8_cap_includes_prompt_schema_and_keeps_whole_findings(self):
        small, huge = finding("b"), finding("a", body="é" * 10000)
        size = len((prepare_selection([small]).prompt + prepare_selection([small]).schema_json).encode("utf-8"))
        inputs = prepare_selection([huge, small], max_input_bytes=size)
        self.assertEqual(inputs.candidates, (small,))
        self.assertEqual(inputs.omitted_ids, ["a"])
        self.assertLessEqual(len((inputs.prompt + inputs.schema_json).encode("utf-8")), size)
        self.assertNotIn(huge.rendered_content, inputs.prompt)

    def test_empty_budget_never_drops_findings(self):
        inputs = prepare_selection([finding("a")], max_input_bytes=1)
        self.assertEqual(inputs.prompt, "")
        self.assertEqual(extract_selection(output(), inputs).retained_ids, ["a"])

    def test_prompt_contains_only_actual_rendered_body(self):
        candidate = finding("a", details="Secret omitted detail that rendering truncated")
        self.assertNotIn("Secret omitted detail", prepare_selection([candidate]).prompt)

    def test_history_supersession_requires_retained_candidate_and_severity(self):
        inputs = prepare_selection([finding("a")], [finding("h")])
        evidence = [dict(candidate_id="a", history_id="h", reason="Covers both timeout and authentication failure.")]
        plan = extract_selection(output(decision("a"), supersessions=evidence), inputs)
        self.assertEqual(plan.superseded_history, {"a": ["h"]})
        with self.assertRaises(SelectionValidationError):
            extract_selection(output(decision("a", "covered", "h", "equivalent"), supersessions=evidence), inputs)

    def test_invalid_types_and_extra_keys_are_rejected(self):
        inputs = prepare_selection([finding("a")])
        invalid = ['{}', '[]', 'not json', output(decision("a"), decision("a")),
                   json.dumps(dict(decisions={}, historical_supersessions=[]))]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(SelectionValidationError):
                extract_selection(raw, inputs)
