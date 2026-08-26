"""``auto_update``: config default and the not-a-checkout escape hatch."""

from tertulia.delegate.__main__ import _auto_update
from tertulia.delegate.config import load_config


def test_auto_update_outside_a_git_repo_is_a_noop(tmp_path):
    assert _auto_update(pkg_dir=tmp_path) is False


def test_auto_update_defaults_off_and_is_opt_in(tmp_path):
    base = tmp_path / "delegate.yaml"
    base.write_text(
        "concierge_url: http://x\nagent_name: A\nowner_name: O\n", encoding="utf-8"
    )
    assert load_config(base).auto_update is False
    base.write_text(
        "concierge_url: http://x\nagent_name: A\nowner_name: O\nauto_update: true\n",
        encoding="utf-8",
    )
    assert load_config(base).auto_update is True
