"""Tests for the possessive merge, the team-page fallback, and the
dashboard's Windows-safe process check."""

import os
from types import SimpleNamespace

from harvey.agents.sender import Sender
from harvey.collectors.profile import ProfileCollector
from harvey.dashboard import _pid_alive


def _render(text, company):
    s = Sender.__new__(Sender)
    prospect = SimpleNamespace(first_name="Dana", last_name="Lee", company=company,
                               title="", personalization_notes="",
                               is_company_inbox=False)
    return s._render(text, prospect)


# ── possessive merge ──

def test_possessive_for_company_ending_in_s():
    assert _render("in {{company}}'s voice", "Parkes Builders") == "in Parkes Builders' voice"


def test_possessive_for_other_companies_unchanged():
    assert _render("in {{company}}'s voice", "Acme Dental") == "in Acme Dental's voice"


# ── team-page fallback ──

def test_profile_detector_reads_heading_plus_role_layout():
    html = ("<h3>Sena Park</h3><p>Practice Manager</p>"
            "<h3>Trusted leadership</h3><p>founder-led since 1999</p>")
    people = ProfileCollector(state=None).detect_people(html)
    names = {(p["first_name"], p["last_name"]) for p in people}
    assert ("Sena", "Park") in names


# ── dashboard process check ──

def test_pid_alive_for_this_process():
    assert _pid_alive(os.getpid())


def test_pid_alive_false_for_missing_process():
    assert not _pid_alive(999_999)
