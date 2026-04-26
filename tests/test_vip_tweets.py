from pathlib import Path
from unittest.mock import MagicMock, patch

from trading_bot.vip_tweets import (
    VipHandle,
    fetch_handle_posts,
    gather_new_posts,
    load_handles,
    load_seen,
    save_seen,
    score,
)


def test_score_high_keyword():
    sev, reason = score("Imposing 50% tariff on China imports")
    assert sev == "high"
    assert "tariff" in reason.lower()


def test_score_med_keyword():
    sev, reason = score("Inflation came in at 3.4% this month")
    assert sev == "med"
    assert "inflation" in reason.lower()


def test_score_low_default():
    sev, _ = score("Hello world, just saying hi")
    assert sev == "low"


def test_score_high_overrides_med():
    """A post containing both HIGH and MED keywords scores HIGH."""
    sev, reason = score("Inflation is up — imposing emergency tariff")
    assert sev == "high"
    assert "tariff" in reason.lower()


def test_load_handles_missing_file_returns_empty(tmp_path):
    assert load_handles(tmp_path / "missing.yaml") == []


def test_load_handles_parses_yaml(tmp_path):
    p = tmp_path / "vip.yaml"
    p.write_text(
        "handles:\n"
        "  - name: Trump\n"
        "    platform: truth_social\n"
        "    rss_url: https://example.com/feed.rss\n"
    )
    handles = load_handles(p)
    assert len(handles) == 1
    assert handles[0].name == "Trump"
    assert handles[0].rss_url == "https://example.com/feed.rss"


def test_save_and_load_seen_roundtrip(tmp_path):
    p = tmp_path / "seen.json"
    save_seen({"Trump": "post-123"}, p)
    assert load_seen(p) == {"Trump": "post-123"}


def test_fetch_handle_posts_parses_rss(tmp_path):
    rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Trump on Truth</title>
<item>
  <guid>https://truthsocial.com/@realDonaldTrump/posts/A</guid>
  <link>https://truthsocial.com/@realDonaldTrump/posts/A</link>
  <title>Imposing 50% tariff on China starting Monday</title>
  <description>SPY will fall</description>
  <pubDate>Mon, 26 Apr 2026 14:00:00 GMT</pubDate>
</item>
<item>
  <guid>https://truthsocial.com/@realDonaldTrump/posts/B</guid>
  <link>https://truthsocial.com/@realDonaldTrump/posts/B</link>
  <title>Just had a great meeting</title>
  <description>Wonderful people</description>
  <pubDate>Mon, 26 Apr 2026 13:00:00 GMT</pubDate>
</item>
</channel></rss>"""

    handle = VipHandle(name="Trump", platform="truth_social", rss_url="https://x/y.rss")

    with patch("trading_bot.vip_tweets.requests.get") as mock_get:
        mock_get.return_value = MagicMock(text=rss, raise_for_status=lambda: None)
        posts = fetch_handle_posts(handle)

    assert len(posts) == 2
    assert posts[0].severity == "high"  # tariff keyword
    assert posts[1].severity == "low"
    assert "tariff" in posts[0].severity_reason.lower()


def test_gather_new_posts_idempotent_after_seen():
    """Re-running with the latest post already in 'seen' returns nothing."""
    handle = VipHandle(name="Trump", platform="truth_social", rss_url="https://x/y.rss")

    rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><guid>id-A</guid><link>l</link><title>tariff alert</title><description></description><pubDate>Mon, 26 Apr 2026 14:00:00 GMT</pubDate></item>
<item><guid>id-B</guid><link>l</link><title>old</title><description></description><pubDate>Mon, 26 Apr 2026 13:00:00 GMT</pubDate></item>
</channel></rss>"""

    with patch("trading_bot.vip_tweets.requests.get") as mock_get:
        mock_get.return_value = MagicMock(text=rss, raise_for_status=lambda: None)

        # First call: empty seen → both posts new
        new1, seen1, _ = gather_new_posts([handle], seen={})
        assert len(new1) == 2
        assert seen1["Trump"] == "id-A"

        # Second call: seen now points at id-A → nothing new
        new2, seen2, _ = gather_new_posts([handle], seen=seen1)
        assert new2 == []
        assert seen2["Trump"] == "id-A"


def test_gather_new_posts_only_new_since_last_seen():
    """If a new post lands above the last-seen one, only that one is returned."""
    handle = VipHandle(name="Trump", platform="truth_social", rss_url="https://x/y.rss")

    rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><guid>id-NEW</guid><link>l</link><title>recession warning</title><description></description><pubDate>Mon, 26 Apr 2026 15:00:00 GMT</pubDate></item>
<item><guid>id-A</guid><link>l</link><title>tariff alert</title><description></description><pubDate>Mon, 26 Apr 2026 14:00:00 GMT</pubDate></item>
<item><guid>id-B</guid><link>l</link><title>old</title><description></description><pubDate>Mon, 26 Apr 2026 13:00:00 GMT</pubDate></item>
</channel></rss>"""

    with patch("trading_bot.vip_tweets.requests.get") as mock_get:
        mock_get.return_value = MagicMock(text=rss, raise_for_status=lambda: None)
        new, seen, _ = gather_new_posts([handle], seen={"Trump": "id-A"})

    assert len(new) == 1
    assert new[0].post_id == "id-NEW"
    assert seen["Trump"] == "id-NEW"


def test_gather_new_posts_handles_fetch_error():
    """A failing handle is reported in errors but doesn't break the rest."""
    bad = VipHandle(name="Bad", platform="truth_social", rss_url="https://broken")
    good = VipHandle(name="Good", platform="truth_social", rss_url="https://ok")

    good_rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><guid>g1</guid><link>l</link><title>hi</title><description></description><pubDate>Mon, 26 Apr 2026 14:00:00 GMT</pubDate></item>
</channel></rss>"""

    def mock_response(url, **kwargs):
        if "broken" in url:
            raise RuntimeError("network down")
        return MagicMock(text=good_rss, raise_for_status=lambda: None)

    with patch("trading_bot.vip_tweets.requests.get", side_effect=mock_response):
        new, seen, errs = gather_new_posts([bad, good], seen={})

    assert len(new) == 1
    assert new[0].handle == "Good"
    assert "Good" in seen
    assert "Bad" not in seen
