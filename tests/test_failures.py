from __future__ import annotations

import unittest

from ariadne_math.failures import fingerprint_failure, novelty_certificate_present


class FailureTests(unittest.TestCase):
    def test_equivalent_failure_wording_clusters(self) -> None:
        a = fingerprint_failure(
            {
                "failure_class": "NONUNIFORM_CONSTANT",
                "signature": "Constant grows with N in the final summation",
                "logical_scope": "absolute-value dyadic estimate",
            }
        )
        b = fingerprint_failure(
            {
                "failure_class": "NONUNIFORM_CONSTANT",
                "signature": "In final summation, the constant grows with N",
                "logical_scope": "dyadic absolute value estimate",
            }
        )
        self.assertEqual(a.canonical_key, b.canonical_key)

    def test_novelty_certificate_requires_difference_and_test(self) -> None:
        self.assertFalse(novelty_certificate_present({"decisive_test": "try it"}))
        self.assertTrue(
            novelty_certificate_present(
                {
                    "representation_difference": "dual variables",
                    "decisive_test": "derive exact dual identity",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
