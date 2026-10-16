"""Pure Discord limits and duration grouping with synthetic catalog rows."""
from collections import Counter

from app.bot_ui import section_pages, duration_heading


def test_catalog_beyond_25_and_empty_groups():
    rows = [{'id': f'daily-{i}', 'duration_group': 'daily', 'description': 'A' * 120} for i in range(64)]
    pages = section_pages(rows, lambda c: f"{c['id']} {c['description']}")
    assert len(pages) > 1
    assert all(len(options) <= 25 and len(body) <= 3500 for options, body in pages)
    assert Counter(c['id'] for options, _ in pages for c in options) == Counter(c['id'] for c in rows)
    assert all('**Monthly**' not in body and '**Yearly**' not in body for _, body in pages)


def test_long_description_not_truncated_and_durations_ordered():
    rows = [{'id': 'year', 'duration_group': 'yearly'}, {'id': 'month', 'duration_group': 'monthly'},
            {'id': 'day', 'duration_group': 'daily'}]
    pages = section_pages(rows, lambda c: c['id'])
    assert [c['id'] for options, _ in pages for c in options] == ['day', 'month', 'year']
    long = 'synthetic-' * 2000
    pages = section_pages([{'id': 'long', 'duration_group': 'monthly'}], lambda c: long)
    assert len(pages) > 1
    assert ''.join(body.replace(duration_heading({'duration_group': 'monthly'}), '') for _, body in pages) == long
    assert all(options[0]['id'] == 'long' for options, _ in pages)


def test_optional_filter_preserves_all_selection():
    rows = [{'id': 'd', 'duration_group': 'daily'}, {'id': 'm', 'duration_group': 'monthly'}]
    filtered = section_pages(rows, lambda c: c['id'], duration='monthly')
    assert [c['id'] for options, _ in filtered for c in options] == ['m']
    assert [c['id'] for options, _ in section_pages(rows, lambda c: c['id']) for c in options] == ['d', 'm']
