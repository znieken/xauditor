from __future__ import annotations

import unittest

from xauditor_portal.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_policy,
    verify_password,
)


class PasswordHashTests(unittest.TestCase):
    def test_hash_is_verifiable(self) -> None:
        h = hash_password("Correct-Horse-Battery-9")
        self.assertTrue(verify_password("Correct-Horse-Battery-9", h))
        self.assertFalse(verify_password("wrong", h))

    def test_hash_is_salted(self) -> None:
        self.assertNotEqual(hash_password("samepass"), hash_password("samepass"))

    def test_verify_rejects_garbage_hash(self) -> None:
        self.assertFalse(verify_password("anything", "not-a-bcrypt-hash"))


class PasswordPolicyTests(unittest.TestCase):
    def test_too_short_is_rejected(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            validate_password_policy("a1a1a1")

    def test_letters_required(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            validate_password_policy("123456789012345")

    def test_digits_required(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            validate_password_policy("abcdefghijklm")

    def test_valid_password_passes(self) -> None:
        validate_password_policy("auditor-can-has-1-chair")

    def test_custom_min_length_honored(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            validate_password_policy("aa1bb", min_length=20)


if __name__ == "__main__":
    unittest.main()
