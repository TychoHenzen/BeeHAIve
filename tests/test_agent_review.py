from beehaiive.demo import demo_review_adapters
from beehaiive.review import REQUIRED_CONCERNS, ReaderStatus


def test_demo_review_adapters_cover_all_required_concerns() -> None:
    provider, readers = demo_review_adapters()
    target = provider.get_pull_request("demo-pr")
    assert target.head_sha == "demo-head"
    assert set(readers) == set(REQUIRED_CONCERNS)
    for concern in REQUIRED_CONCERNS:
        assert readers[concern].review(target).status is ReaderStatus.PASS
