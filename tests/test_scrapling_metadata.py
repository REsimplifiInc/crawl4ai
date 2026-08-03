from __future__ import annotations

from pathlib import Path


def test_scrapling_extra_and_lxml_range_are_declared():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()

    assert '"lxml>=5.3,<7"' in pyproject
    assert '"scrapling[fetchers]>=0.4.9,<0.4.10"' in pyproject
    assert '"apify-fingerprint-datapoints==0.13.0"' in pyproject
    assert '"scrapling[fetchers]>=0.4.9,<0.4.10"' in pyproject
