import unittest

from scout.comment_request import (
    CommentRequestValidationError,
    build_comment_request_prompt,
    comment_request_schema_json,
    extract_comment_request,
    has_scout_mention,
)


class CommentRequestTests(unittest.TestCase):
    def test_scout_mention_prefilter_is_case_insensitive_and_local(self):
        self.assertTrue(has_scout_mention("@Scout please review this PR"))
        self.assertTrue(has_scout_mention("cc @scout: can you scan this?"))
        self.assertFalse(has_scout_mention("@scout-bot please review"))
        self.assertFalse(has_scout_mention("alice@scout.example"))

    def test_prompt_and_schema_describe_strict_output(self):
        prompt = build_comment_request_prompt("@scout review this")
        schema = comment_request_schema_json()

        self.assertIn("@scout review this", prompt)
        self.assertIn("review_requested", schema)
        self.assertIn("additionalProperties", schema)

    def test_extract_comment_request_from_schema_json(self):
        result = extract_comment_request('{"review_requested":true,"reason":"explicit review request"}')

        self.assertTrue(result.review_requested)
        self.assertEqual(result.reason, "explicit review request")

    def test_extract_comment_request_from_provider_result_envelope(self):
        result = extract_comment_request(
            '{"type":"result","result":"{\\"review_requested\\":false,\\"reason\\":\\"status check\\"}"}'
        )

        self.assertFalse(result.review_requested)
        self.assertEqual(result.reason, "status check")

    def test_extract_comment_request_rejects_invalid_or_extra_fields(self):
        with self.assertRaises(CommentRequestValidationError):
            extract_comment_request('{"review_requested":"yes","reason":"bad"}')
        with self.assertRaises(CommentRequestValidationError):
            extract_comment_request('{"review_requested":true,"reason":"ok","extra":1}')


if __name__ == "__main__":
    unittest.main()
