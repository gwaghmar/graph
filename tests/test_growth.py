import os; os.environ.setdefault("GRAPH_SKILL_NO_PROBES", "1")  # never call real CLIs (vercel whoami opens browser sign-in)
import tempfile as _tf; os.environ.setdefault("GRAPH_SKILL_CONFIG_DIR", _tf.mkdtemp(prefix="graph-perm-"))  # never touch the real ~/.config
import calendar, contextlib, io, json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

CORE = Path(__file__).resolve().parent.parent / 'skills' / 'graph' / 'scripts'
GRAPH = str(CORE / 'graph.py')
sys.path.insert(0, str(CORE))
import growth  # noqa: E402

T0 = calendar.timegm((2026, 11, 10, 12, 0, 0))
DAY = 86400
D0 = '2026-11-10'


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(); self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._cwd = os.getcwd(); os.chdir(self.dir); self.addCleanup(os.chdir, self._cwd)
        self.t = T0
        p = mock.patch.object(growth, '_clock', lambda: self.t); p.start(); self.addCleanup(p.stop)
        self.hooks = {}

    def g(self, *argv, code=0, hooks=None):
        out, err = io.StringIO(), io.StringIO()
        got = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try: growth.dispatch(list(argv), hooks if hooks is not None else self.hooks)
            except SystemExit as e: got = e.code
        self.assertEqual(got, code, f'{argv}: {out.getvalue()}{err.getvalue()}')
        return out.getvalue() + err.getvalue()

    def camp(self):
        return json.loads((self.dir / '.graph/campaigns/c1/campaign.json').read_text(encoding='utf-8'))

    def logs(self):
        return [json.loads(l) for l in (self.dir / '.graph/campaigns/c1/log.jsonl').read_text(encoding='utf-8').splitlines()]

    def draft(self, name, text):
        p = self.dir / '.graph/campaigns/c1/drafts' / name
        p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding='utf-8'); return f'drafts/{name}'

    def setup_campaign(self, *channel_extra, baseline=True, exps=1):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars', '--app', 'https://x.dev')
        self.g('source', 'add', 'stars', '--via', 'manual')
        self.g('channel', 'add', 'x', '--handle', '@me', '--daily-cap', '2', '--min-gap-min', '30', *channel_extra)
        self.g('read', '--metric', 'stars', '--value', '10', '--as-of', D0)
        for i in range(exps):
            self.g('exp', 'add', '--hypothesis', 'a post helps', '--surface', f's{i + 1}', '--change-kind', 'post', '--change-ref', f'post {i + 1}', '--metric', 'stars', '--window-days', '7')
            if baseline: self.g('exp', 'baseline', f'e{i + 1}')

    def queue(self, text='Hello from my own account', eid='e1', *extra, code=0, name=None):
        d = self.draft(name or f'{eid}.md', text)
        return self.g('queue', 'add', '--experiment', eid, '--channel', 'x', '--kind', 'post', '--draft', d, '--preview', text, '--rung', '1', *extra, code=code)

    def item(self, qid='q1'):
        rows = [json.loads(l) for l in (self.dir / '.graph/campaigns/c1/queue.jsonl').read_text(encoding='utf-8').splitlines()]
        return [r for r in rows if r['id'] == qid][-1]

    def claim(self, qid='q1', *extra, code=0):
        out = self.g('post', qid, '--claim', *extra, code=code)
        m = re.search(r'Token (\w+)', out); return m.group(1) if m else None

    def post(self, qid='q1', result='https://x.com/me/1', *extra, claim_code=0, before=None, thread=None):
        """Claim the slot, then confirm. claim_code is the expected exit of the claim."""
        tok = self.claim(qid, *extra, *(['--thread', thread] if thread else []), code=claim_code)
        if claim_code: return None
        return self.g('post', qid, '--confirm', tok, '--result', result, *(['--before-shot', before] if before else []), *(['--thread', thread] if thread else []))

    def approve(self, qid='q1', words='yes, post it'):
        self.g('queue', 'show')
        self.g('queue', 'approve', qid, '--words', words, '--hash', growth.item_hash(self.item(qid), 'c1')[:12])


class StateMachine(Base):
    def test_one_change_per_experiment(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars')
        a = ('exp', 'add', '--hypothesis', 'h', '--surface', 's', '--change-kind', 'post', '--metric', 'stars')
        self.g(*a, '--change-ref', 'one', '--change-ref', 'two', code=3)
        self.g(*a, '--change-ref', 'one; two', code=3)
        self.g(*a, '--change-ref', 'one')

    def test_no_live_without_dated_baseline(self):
        self.setup_campaign(baseline=False)
        self.g('exp', 'start', 'e1', code=3)
        self.g('exp', 'baseline', 'e1')
        self.g('read', '--metric', 'stars', '--value', '12', '--as-of', '2026-11-11', '--source', 'user typed', '--method', 'typed')
        self.g('exp', 'add', '--hypothesis', 'h', '--surface', 'z', '--change-kind', 'post', '--change-ref', 'p', '--metric', 'stars')
        self.g('exp', 'baseline', 'e2'); self.g('exp', 'start', 'e2', code=3)  # baseline dated after the change
        self.g('exp', 'start', 'e1')
        self.assertEqual(self.camp()['experiments'][0]['state'], 'live')

    def test_future_reading_is_refused(self):
        self.setup_campaign()
        self.g('read', '--metric', 'stars', '--value', '999', '--as-of', '2030-01-01', '--source', 'model', '--method', 'guess', code=3)
        (self.dir / '.graph/campaigns/c1/inbox/f.csv').write_text('metric,date,value\nstars,2030-01-01,999\nstars,2026-11-09,9\n', encoding='utf-8')
        self.g('source', 'add', 'stars', '--via', 'csv'); self.g('read', '--metric', 'stars')
        self.assertEqual([r['as_of'] for r in growth.readings('c1')], [D0, '2026-11-09'])

    def test_baseline_refuses_unknown_and_stale(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars')
        self.g('source', 'add', 'stars', '--via', 'manual')
        self.g('exp', 'add', '--hypothesis', 'h', '--surface', 's', '--change-kind', 'post', '--change-ref', 'p', '--metric', 'stars')
        self.g('exp', 'baseline', 'e1', code=3)  # no reading at all
        self.g('read', '--metric', 'stars', '--unknown')
        self.g('exp', 'baseline', 'e1', code=3)  # unknown is not a number
        self.g('read', '--metric', 'stars', '--value', '5', '--as-of', '2026-11-01')
        self.g('exp', 'baseline', 'e1', code=3)  # 9 days old, lag 3

    def test_one_live_per_metric_and_surface(self):
        self.setup_campaign(exps=2)
        self.g('exp', 'add', '--hypothesis', 'h', '--surface', 's1', '--change-kind', 'post', '--change-ref', 'again', '--metric', 'stars')
        self.g('exp', 'baseline', 'e3')
        self.g('exp', 'start', 'e1'); self.g('exp', 'start', 'e2')  # other surface is fine
        self.g('exp', 'start', 'e3', code=3)

    def test_no_decide_before_window_end(self):
        self.setup_campaign(); self.g('exp', 'start', 'e1')
        self.g('exp', 'decide', 'e1', '--decision', 'inconclusive', '--reason', 'early', code=3)
        self.t += 7 * DAY + 60
        self.g('read', '--metric', 'stars', '--value', '30', '--as-of', '2026-11-17')
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'went up')
        e = self.camp()['experiments'][0]
        self.assertEqual(e['state'], 'decided'); self.assertEqual(e['decision']['reading']['value'], 30)
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'again', code=3)

    def test_decision_needs_a_reading_from_after_the_window_and_not_the_baseline(self):
        self.setup_campaign(); self.g('exp', 'start', 'e1')
        self.t += 7 * DAY + 60                                    # window ended 2026-11-17 12:00
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'x', '--reading', 'r1', code=3)  # r1 is the baseline
        self.g('read', '--metric', 'stars', '--value', '11', '--as-of', '2026-11-16')                    # day before the end: too early
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'x', code=3)
        self.g('read', '--metric', 'stars', '--value', '10', '--as-of', '2026-11-17')                    # end day, date only: counts
        self.g('read', '--metric', 'stars', '--value', '10', '--as-of', '2026-11-17', '--source', 'user typed', '--method', 'again')
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'x', '--reading', 'r3', code=3)  # not the latest, no reason
        out = self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'x', '--reading', 'r3', '--why-not-latest', 'r4 is a duplicate')
        self.assertIn('did not move', out)

    def test_below_min_sample_only_inconclusive(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars'); self.g('source', 'add', 'stars', '--via', 'manual')
        self.g('read', '--metric', 'stars', '--value', '10', '--as-of', D0)
        self.g('exp', 'add', '--hypothesis', 'h', '--surface', 's', '--change-kind', 'post', '--change-ref', 'p', '--metric', 'stars', '--min-sample', '100')
        self.g('exp', 'baseline', 'e1'); self.g('exp', 'start', 'e1'); self.t += 8 * DAY
        self.g('read', '--metric', 'stars', '--value', '40', '--as-of', '2026-11-18', '--sample', '20')
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'up', code=3)
        self.g('exp', 'decide', 'e1', '--decision', 'inconclusive', '--reason', 'sample too small')

    def test_stale_and_unknown_readings_cannot_support_a_decision(self):
        self.setup_campaign(); self.g('exp', 'start', 'e1'); self.t += 12 * DAY
        self.g('read', '--metric', 'stars', '--value', '30', '--as-of', '2026-11-11')  # 8 days old now, lag 3
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'up', code=3)
        self.g('read', '--metric', 'stars', '--unknown')
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'up', code=3)
        self.g('exp', 'decide', 'e1', '--decision', 'inconclusive', '--reason', 'no fresh number')

    def test_revert_needs_revert_ref_and_undo(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars'); self.g('source', 'add', 'stars', '--via', 'manual')
        self.g('read', '--metric', 'stars', '--value', '10', '--as-of', D0)
        mk = lambda s, kind, *x: self.g('exp', 'add', '--hypothesis', 'h', '--surface', s, '--change-kind', kind, '--change-ref', 'r', '--metric', 'stars', '--window-days', '1', *x)
        mk('a', 'copy'); mk('b', 'copy', '--revert-ref', 'old title'); mk('c', 'post')
        for e in ('e1', 'e2', 'e3'): self.g('exp', 'baseline', e); self.g('exp', 'start', e)
        self.t += 2 * DAY
        self.g('read', '--metric', 'stars', '--value', '3', '--as-of', '2026-11-12')
        self.g('exp', 'decide', 'e1', '--decision', 'revert', '--reason', 'down', '--undo', 'did it', code=3)  # no revert_ref
        self.g('exp', 'decide', 'e2', '--decision', 'revert', '--reason', 'down', code=3)  # no undo
        self.g('exp', 'decide', 'e2', '--decision', 'revert', '--reason', 'down', '--undo', 'restored old title')
        self.g('exp', 'decide', 'e3', '--decision', 'revert', '--reason', 'down', '--undo', 'stopped the pattern')
        self.assertEqual(self.camp()['experiments'][2]['change']['revert_ref'], 'stop the pattern')
        self.assertEqual(self.camp()['experiments'][1]['decision']['undo'], 'restored old title')

    def test_readings_need_source_method_as_of_and_unknown_is_not_zero(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars'); self.g('source', 'add', 'stars', '--via', 'cli')
        self.g('read', '--metric', 'stars', '--value', '5', code=3)
        self.g('read', '--metric', 'stars', '--value', '5', '--source', 'cli x', '--method', 'ran it', '--as-of', 'yesterday', code=3)
        self.g('read', '--metric', 'stars', '--unknown', '--source', 'none', '--method', 'not read', '--as-of', D0)
        rows = [json.loads(l) for l in (self.dir / '.graph/campaigns/c1/metrics.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 1); self.assertIsNone(rows[0]['value'])


class Sources(Base):
    def test_public_github_stars_and_failure(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars')
        self.g('source', 'add', 'stars', '--via', 'public', '--ref', 'github:o/r')
        with mock.patch.object(growth.outside, 'get_json', return_value=({'stargazers_count': 42}, None)):
            out = self.g('read', '--metric', 'stars')
        self.assertIn('42 (source GitHub o/r', out); self.assertIn('as of 2026-11-10', out)
        with mock.patch.object(growth.outside, 'get_json', return_value=(None, 'network failed')):
            out = self.g('read', '--metric', 'stars')
        self.assertIn('stars: unknown', out)
        self.assertIsNone(growth.readings('c1')[-1]['value'])

    def test_csv_inbox(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars', '--metric', 'visits')
        self.g('source', 'add', 'visits', '--via', 'csv')
        (self.dir / '.graph/campaigns/c1/inbox/export.csv').write_text('metric,date,value,sample\nvisits,2026-11-03,100,100\nvisits,2026-11-10,abc,\nother,2026-11-10,5,\n', encoding='utf-8')
        self.g('read', '--metric', 'visits'); self.g('read', '--metric', 'visits')  # second read adds nothing
        rows = growth.readings('c1')
        self.assertEqual([(r['as_of'], r['value']) for r in rows], [('2026-11-03', 100), ('2026-11-10', None)])
        self.assertTrue(all(r['source'] == 'csv inbox/export.csv' for r in rows))


class Queue(Base):
    def test_approval_bound_to_hash_and_edits_need_new_tap(self):
        self.setup_campaign(); self.queue()
        self.g('queue', 'approve', 'q1', '--words', 'yes', '--hash', self.item()['sha256'][:12], code=3)  # never shown
        self.g('queue', 'show')
        self.g('queue', 'approve', 'q1', '--words', 'yes', '--hash', 'deadbeef00', code=3)
        self.g('queue', 'approve', 'q1', '--words', 'Hello from my own account', '--hash', self.item()['sha256'][:12], code=3)  # words are the draft
        self.g('queue', 'approve', 'q1', '--words', 'yes', '--hash', self.item()['sha256'][:12], '--auto-approve', code=3)
        self.g('queue', 'approve', 'q1', '--words', 'yes, post it', '--hash', self.item()['sha256'][:12])
        self.assertEqual(self.item()['approved']['words'], 'yes, post it')
        (self.dir / '.graph/campaigns/c1/drafts/e1.md').write_text('Edited text after approval', encoding='utf-8')
        self.claim('q1', '--rung', '1', code=3)
        self.assertEqual(self.item()['state'], 'drafted'); self.assertIsNone(self.item()['approved'])
        self.claim('q1', '--rung', '1', code=3)  # still not approved

    def test_unapproved_and_dropped_never_post(self):
        self.setup_campaign(); self.queue()
        self.claim('q1', '--rung', '1', code=3)
        self.g('queue', 'drop', 'q1')
        self.claim('q1', '--rung', '1', code=3)
        self.g('post', 'q1', '--result', 'https://x.com/me/1', code=3)  # no claim, no confirm: nothing is recorded

    def test_expiry_means_never_posted(self):
        self.setup_campaign(); self.queue(); self.approve()
        self.t += 73 * 3600
        self.claim('q1', '--rung', '1', code=3)
        self.assertEqual(self.item()['state'], 'expired')
        self.assertEqual(growth.checkpoint_cards('c1'), [])

    def test_unanswered_expires_on_tick_and_cannot_be_approved(self):
        self.setup_campaign(); self.queue(); self.g('queue', 'show')
        self.t += 80 * 3600
        self.g('queue', 'approve', 'q1', '--words', 'yes', '--hash', self.item()['sha256'][:12], code=3)
        self.assertEqual(self.item()['state'], 'expired')

    def test_duplicate_text_and_one_item_per_experiment(self):
        self.setup_campaign(exps=2); self.queue('Same words here', 'e1')
        self.queue('same   WORDS here', 'e2', code=3)
        self.queue('Other words', 'e1', code=3, name='x.md')  # e1 already has an item
        self.queue('Other words', 'e2', name='y.md')

    def test_own_accounts_only_reply_thread_and_disclosure(self):
        self.setup_campaign('--disclosure', 'I built this')
        d = self.draft('r.md', 'Nice question. I built this tool.\nthread: https://x.com/a/status/1')
        base = ['queue', 'add', '--experiment', 'e1', '--channel', 'x', '--draft', d, '--rung', '1']
        self.g(*base, '--kind', 'post', '--handle', '@someone', '--disclosure', 'I built this', code=3)  # not my account
        self.g(*base, '--kind', 'post', '--disclosure', 'something else', code=3)
        self.g(*base, '--kind', 'post', code=3)  # no disclosure given
        d2 = self.draft('r2.md', 'No thread line. I built this')
        self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--draft', d2, '--rung', '1', '--kind', 'reply', '--disclosure', 'I built this', code=3)
        self.g(*base, '--kind', 'reply', '--disclosure', 'I built this')
        self.approve()
        self.claim('q1', '--rung', '1', '--thread', 'https://x.com/other/status/9', code=3)
        self.claim('q1', '--rung', '1', code=3)
        self.post('q1', 'https://x.com/me/2', '--rung', '1', thread='https://x.com/a/status/1')

    def test_edited_draft_equal_to_a_posted_text_is_refused_at_claim(self):
        self.setup_campaign(exps=2); self.queue('First post text', 'e1', name='1.md'); self.queue('Second post text', 'e2', name='2.md')
        self.approve('q1'); self.post('q1', 'https://x.com/me/1', '--rung', '1')
        (self.dir / '.graph/campaigns/c1/drafts/2.md').write_text('first POST text', encoding='utf-8')
        self.approve('q2'); self.t += 31 * 60
        self.claim('q2', '--rung', '1', code=3)
        self.assertNotEqual(self.item('q2')['state'], 'posted')
        (self.dir / '.graph/campaigns/c1/drafts/1.md').unlink()  # a deleted draft cannot hide the duplicate
        self.queue('first post text', 'e1', code=3, name='3.md')

    def test_disclosure_must_be_the_channels_own_words(self):
        self.setup_campaign('--disclosure', '#ad')
        d = self.draft('d.md', 'Check out the new release')
        self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', d, '--rung', '1', '--disclosure', 'the', code=3)
        d2 = self.draft('d2.md', 'Check out the new release #AD')
        self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', d2, '--rung', '1', '--disclosure', 'the', code=3)
        self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', d2, '--rung', '1', '--disclosure', '#ad')

    def test_claim_is_one_time_and_bound_to_the_draft(self):
        self.setup_campaign(); self.queue(); self.approve()
        tok = self.claim('q1', '--rung', '1')
        self.claim('q1', '--rung', '1', code=3)  # already claimed
        (self.dir / '.graph/campaigns/c1/drafts/e1.md').write_text('different text', encoding='utf-8')
        self.g('post', 'q1', '--confirm', tok, '--result', 'https://x.com/me/1', code=3)
        self.assertEqual(self.item()['state'], 'claimed')
        self.g('post', 'q1', '--failed', '--why', 'edited after claim')

    def test_stale_claim_is_refused(self):
        self.setup_campaign(); self.queue(); self.approve()
        tok = self.claim('q1', '--rung', '1'); self.t += 31 * 60
        self.g('post', 'q1', '--confirm', tok, '--result', 'https://x.com/me/1', code=3)

    def test_screenshot_proof_must_be_an_image(self):
        self.setup_campaign(); self.queue(); self.approve()
        tok = self.claim('q1', '--rung', '1')
        self.g('post', 'q1', '--confirm', tok, '--result', str(self.dir / '.graph/campaigns/c1/drafts/e1.md'), code=3)

    def test_channel_loosening_needs_the_users_words_and_cap_is_per_project(self):
        self.setup_campaign()
        self.g('channel', 'add', 'x', '--handle', '@me', '--daily-cap', '9', '--min-gap-min', '30', code=3)
        self.g('channel', 'add', 'x', '--handle', '@me', '--handle', '@other', '--daily-cap', '2', '--min-gap-min', '30', code=3)
        self.g('channel', 'add', 'x', '--handle', '@me', '--daily-cap', '1', '--min-gap-min', '60')  # tighter is fine
        self.g('channel', 'add', 'x', '--handle', '@me', '--daily-cap', '2', '--min-gap-min', '60', '--words', 'raise it to 2')
        self.assertEqual(self.camp()['channels']['x']['changes'][0]['words'], 'raise it to 2')
        self.assertTrue(any(l['cmd'] == 'channel' and l['outcome'] == 'refused' for l in self.logs()))
        # a second campaign on the same channel shares the same ledger
        self.queue(); self.approve(); self.post('q1', 'https://x.com/me/1', '--rung', '1')
        self.g('init', 'Other goal', '--id', 'c2', '--metric', 'stars'); self.g('source', 'add', 'stars', '--via', 'manual', '--campaign', 'c2')
        self.g('channel', 'add', 'x', '--handle', '@me', '--daily-cap', '2', '--min-gap-min', '60', '--campaign', 'c2')
        self.assertEqual(len(growth.ledger_rows()), 1)

    def test_draft_must_live_in_drafts_folder(self):
        self.setup_campaign()
        (self.dir / 'outside.md').write_text('x', encoding='utf-8')
        self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', '../../../outside.md', '--rung', '1', code=3)

    def test_max_five_per_cycle(self):
        self.setup_campaign(exps=6)
        for i in range(1, 6): self.queue(f'text {i}', f'e{i}', name=f'{i}.md')
        self.queue('text 6', 'e6', code=3, name='6.md')

    def test_daily_cap_and_min_gap(self):
        self.setup_campaign(exps=3)
        for i in (1, 2, 3): self.queue(f'text {i}', f'e{i}', name=f'{i}.md'); self.approve(f'q{i}')
        self.post('q1', 'https://x.com/me/1', '--rung', '1')
        self.claim('q2', '--rung', '1', code=3)  # gap 30 min
        self.t += 31 * 60
        self.post('q2', 'https://x.com/me/2', '--rung', '1')
        self.t += 31 * 60
        self.claim('q3', '--rung', '1', code=3)  # cap 2 a day

    def test_ladder_recorded_per_post(self):
        self.setup_campaign(exps=3)
        for i in (1, 2, 3): self.queue(f'text {i}', f'e{i}', name=f'{i}.md'); self.approve(f'q{i}')
        self.claim('q1', '--rung', '2', code=3)  # rung 2 needs a why
        self.post('q1', 'https://x.com/me/1', '--rung', '2', '--why', 'no api key on this account')
        self.assertEqual(self.item('q1')['posted']['rung'], 2)
        self.assertEqual(self.item('q1')['result']['url'], 'https://x.com/me/1')
        self.assertEqual(self.camp()['experiments'][0]['state'], 'live')  # the post is the change
        self.t += 31 * 60
        shot = self.dir / 'after.png'; shot.write_bytes(b'png'); before = self.dir / 'before.png'; before.write_bytes(b'png2')
        w = ('--rung', '4', '--why', 'site has no api or browser tool')
        tok = self.claim('q2', *w)
        self.g('post', 'q2', '--confirm', tok, '--result', str(shot), code=3)  # no before shot
        self.g('post', 'q2', '--confirm', tok, '--before-shot', str(before), '--result', 'https://x.com/me/2', code=3)  # url is not an after shot
        self.g('post', 'q2', '--confirm', tok, '--before-shot', str(shot), '--result', str(shot), code=3)  # same file twice
        self.g('post', 'q2', '--confirm', 'wrongtoken', '--before-shot', str(before), '--result', str(shot), code=3)
        self.g('post', 'q2', '--confirm', tok, '--before-shot', str(before), '--result', str(shot))
        self.assertEqual(self.item('q2')['posted']['before_sha256'], growth.sha_file(before))
        self.assertEqual(self.item('q2')['posted']['rung'], 4); self.assertIn('screenshot', self.item('q2')['result'])
        self.g('post', 'q3', '--claim', '--rung', '9', code=2)

    def test_failed_post_is_recorded(self):
        self.setup_campaign(); self.queue(); self.approve()
        self.g('post', 'q1', '--failed', '--why', 'site was down')
        self.assertEqual(self.item()['state'], 'failed')

    def test_failed_after_claim_still_counts_toward_gap_but_not_cap(self):
        self.setup_campaign(exps=2); self.queue('one', 'e1', name='1.md'); self.queue('two', 'e2', name='2.md'); self.approve('q1'); self.approve('q2')
        self.claim('q1', '--rung', '1'); self.g('post', 'q1', '--failed', '--why', 'maybe half posted')
        self.claim('q2', '--rung', '1', code=3)  # gap
        self.t += 31 * 60
        self.claim('q2', '--rung', '1')

    def test_post_without_baseline_is_refused(self):
        self.setup_campaign(baseline=False)
        self.queue(code=3)

    def test_checkpoint_cards_and_go(self):
        self.setup_campaign(exps=2); self.queue('one', 'e1', name='1.md'); self.queue('two', 'e2', name='2.md')
        cards = growth.checkpoint_cards('c1')
        self.assertEqual(len(cards), 2); self.assertTrue(all(c['id'].startswith('queue:q') for c in cards))
        self.assertEqual(self.item()['state'], 'shown')
        growth.checkpoint_apply('c1', [cards[0]['id']], [cards[1]['id']])
        self.assertEqual(self.item('q1')['state'], 'approved'); self.assertTrue(self.item('q1')['approved']['pressed-go'])
        self.assertEqual(self.item('q2')['state'], 'dropped')

    def test_card_shows_the_draft_not_the_models_preview(self):
        self.setup_campaign()
        d = self.draft('a.md', 'Totally different text than the label')
        self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', d, '--preview', 'Harmless short note', '--rung', '1')
        card = growth.checkpoint_cards('c1')[0]
        self.assertIn('Totally different text', card['title']); self.assertNotIn('Harmless', card['title'])
        out = self.g('queue', 'show'); self.assertIn('Totally different text', out)
        (self.dir / '.graph/campaigns/c1/drafts/a.md').write_text('Edited text after it was shown', encoding='utf-8')
        self.g('queue', 'approve', 'q1', '--words', 'yes', '--hash', self.item()['sha256'][:12], code=3)  # hash shown is not the current one
        self.g('queue', 'show')
        self.assertIn('Edited text', self.g('queue', 'show'))
        self.g('queue', 'approve', 'q1', '--words', 'yes', '--hash', growth.item_hash(self.item(), 'c1')[:12])

    def test_only_displayed_cards_are_marked_shown(self):
        self.setup_campaign(exps=6)
        for i in range(1, 6): self.queue(f'text {i}', f'e{i}', name=f'{i}.md')
        self.t += 80 * 3600  # all expire; a sixth cannot be added in the same cycle, so move the cycle on
        c = self.camp(); c['cycle'] += 1; (self.dir / '.graph/campaigns/c1/campaign.json').write_text(json.dumps(c), encoding='utf-8')
        for i in range(5, 7): self.queue(f'new {i}', f'e{i}', name=f'n{i}.md')
        self.assertEqual(len(growth.checkpoint_cards('c1', mark=False)), 2)
        self.assertEqual(self.item('q6')['state'], 'drafted')

    def test_go_approves_only_cards_the_page_showed(self):
        self.setup_campaign(exps=2); self.queue('one', 'e1', name='1.md')
        cards = growth.checkpoint_cards('c1')
        self.queue('two', 'e2', name='2.md')  # added after the page was drawn
        self.assertEqual(self.item('q2')['state'], 'drafted')
        growth.checkpoint_apply('c1', [cards[0]['id']], [])
        self.assertEqual(self.item('q1')['state'], 'approved'); self.assertEqual(self.item('q2')['state'], 'drafted')
        with self.assertRaises(growth.Refuse): growth.checkpoint_apply('c1', ['queue:q2:' + growth.item_hash(self.item('q2'), 'c1')[:12]], [])  # never shown
        self.assertEqual(self.item('q2')['state'], 'drafted')

    def test_checkpoint_refuses_edited_draft(self):
        self.setup_campaign(); self.queue()
        cards = growth.checkpoint_cards('c1')
        (self.dir / '.graph/campaigns/c1/drafts/e1.md').write_text('changed after it was shown', encoding='utf-8')
        with self.assertRaises(growth.Refuse): growth.checkpoint_apply('c1', [cards[0]['id']], [])
        self.assertEqual(self.item()['state'], 'shown')


class Video(Base):
    def test_video_check_rules(self):
        f = self.dir / 'out.mp4'
        self.g('video-check', str(f), code=3)  # missing
        f.write_bytes(b'')
        self.g('video-check', str(f), code=3)  # empty
        f.write_bytes(b'x' * 10)
        with mock.patch.object(growth, 'have', return_value=False):
            out = self.g('video-check', str(f), '--thumb', str(self.dir / 't.png'))
        self.assertIn('size only', out); self.assertIn('No thumbnail', out)
        fake = lambda dur: mock.Mock(stdout=dur + '\n', returncode=0)
        with mock.patch.object(growth, 'have', return_value=True), mock.patch.object(growth.subprocess, 'run', return_value=fake('0.000000')):
            self.g('video-check', str(f), code=3)  # duration 0
        with mock.patch.object(growth, 'have', return_value=True), mock.patch.object(growth.subprocess, 'run', return_value=fake('2.5')):
            self.assertIn('duration 2.5s', self.g('video-check', str(f)))
        with mock.patch.object(growth, 'have', return_value=True), mock.patch.object(growth.subprocess, 'run', return_value=fake('N/A')):
            self.g('video-check', str(f), code=3)

    def test_video_card_with_thumbnail_and_media_in_hash(self):
        self.setup_campaign()
        vid = self.dir / 'v.mp4'; vid.write_bytes(b'video-one'); th = self.dir / 'v.png'; th.write_bytes(b'png')
        d = self.draft('v.md', 'Watch the demo')
        with mock.patch.object(growth, 'have', return_value=False):
            self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'video', '--draft', d, '--rung', '3', '--why-not-higher', 'no api', '--media', str(vid), '--thumb', str(th))
            self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'video', '--draft', d, '--rung', '3', '--why-not-higher', 'x', code=3)  # no media
        self.assertEqual(growth.checkpoint_cards('c1')[0]['thumb'], str(th))
        self.approve()
        vid.write_bytes(b'video-two')  # same caption, different video
        self.claim('q1', '--rung', '3', '--why', 'no api', code=3)

    def test_rung_above_one_needs_reason_at_queue_time(self):
        self.setup_campaign(); d = self.draft('a.md', 'hi')
        self.g('queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', d, '--rung', '3', code=3)


class TickAndReport(Base):
    def make_hooks(self, left=None, status=None):
        calls = []
        h = {'host': 'claude-code', 'open_run': lambda task, rid, cid: calls.append(rid), 'run_status': lambda r: status,
             'budget': lambda: {'status': 'ok', 'left_pct': left} if left is not None else {'status': 'unknown'}}
        return h, calls

    def test_tick_order_logged_and_never_posts(self):
        self.setup_campaign(); self.queue(); self.approve()
        h, calls = self.make_hooks(left=80)
        out = self.g('tick', hooks=h)
        self.assertEqual(calls, ['c1-c1']); self.assertIn('NOTHING WAS POSTED', out); self.assertIn('/loop', out)
        self.assertEqual(self.logs()[-1]['steps'], ['load', 'budget', 'read', 'decide', 'expire', 'open-run', 'draft', 'checkpoint'])
        self.assertEqual(self.item()['state'], 'approved')  # never posted by tick

    def test_tick_budget_gate_refuses_and_logs(self):
        self.setup_campaign(); h, calls = self.make_hooks(left=4)
        self.g('tick', code=3, hooks=h)
        self.assertEqual(calls, []); self.assertEqual(self.logs()[-1]['outcome'], 'refused')
        self.assertEqual(self.logs()[-1]['steps'], ['load', 'budget'])

    def test_tick_reuses_open_cycle_run_and_expires_queue(self):
        self.setup_campaign(); self.queue(); h, calls = self.make_hooks(status='planning')
        self.g('tick', hooks=h); self.g('tick', hooks=h)
        self.assertEqual(calls, ['c1-c1'])  # first opens, second sees one open (status stub says planning)
        self.t += 80 * 3600
        self.g('tick', hooks=h)
        self.assertEqual(self.item()['state'], 'expired')

    def test_tick_closes_ended_experiment_with_too_little_data_and_schedule_hints(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars'); self.g('source', 'add', 'stars', '--via', 'manual')
        self.g('read', '--metric', 'stars', '--value', '10', '--as-of', D0)
        self.g('exp', 'add', '--hypothesis', 'h', '--surface', 's', '--change-kind', 'post', '--change-ref', 'p', '--metric', 'stars', '--window-days', '1', '--min-sample', '50')
        self.g('exp', 'baseline', 'e1'); self.g('exp', 'start', 'e1'); self.t += 2 * DAY
        self.g('read', '--metric', 'stars', '--value', '11', '--as-of', '2026-11-12', '--sample', '5')
        h, _ = self.make_hooks(); h['host'] = 'codex'
        out = self.g('tick', hooks=h)
        self.assertIn('Codex automation', out)
        self.assertEqual(self.camp()['experiments'][0]['decision']['decision'], 'inconclusive')
        h['host'] = 'unknown'; self.assertIn('cron', self.g('tick', hooks=h))

    def test_every_command_and_refusal_is_logged(self):
        self.setup_campaign(); self.g('exp', 'start', 'e1'); self.g('exp', 'start', 'e1', code=3)
        cmds = [(l['cmd'], l['outcome']) for l in self.logs()]
        self.assertIn(('init', 'ok'), cmds); self.assertIn(('channel', 'ok'), cmds); self.assertIn(('exp add', 'ok'), cmds)
        self.assertEqual(cmds[-1], ('exp start', 'refused')); self.assertIn('message', self.logs()[-1])

    def test_report_is_honest(self):
        self.g('init', 'Grow stars', '--id', 'c1', '--metric', 'stars', '--metric', 'visits'); self.g('source', 'add', 'stars', '--via', 'manual')
        for d, v in (('2026-11-01', 4), ('2026-11-02', 5), ('2026-11-03', 6)): self.g('read', '--metric', 'stars', '--value', str(v), '--as-of', d)
        self.g('read', '--metric', 'stars', '--value', '10', '--as-of', '2026-11-10')
        self.g('read', '--metric', 'stars', '--value', '8', '--as-of', '2026-11-03', '--source', 'csv', '--method', 'import')
        self.g('exp', 'add', '--hypothesis', 'h', '--surface', 's', '--change-kind', 'post', '--change-ref', 'p', '--metric', 'stars', '--window-days', '1')
        self.g('exp', 'baseline', 'e1'); self.g('exp', 'start', 'e1')
        out = self.g('report')
        self.assertIn('Not enough data to decide yet', out)
        self.assertIn('UNKNOWN', out); self.assertIn('visits', out.split('## Not known')[1])
        self.assertIn('Same weekday last week', out); self.assertIn('Prior trend', out)
        self.t += 2 * DAY
        self.g('read', '--metric', 'stars', '--value', '20', '--as-of', '2026-11-12')
        self.g('exp', 'decide', 'e1', '--decision', 'keep', '--reason', 'rose after the post')
        out = self.g('report')
        self.assertIn('moved with the change', out); self.assertNotIn('caused', out.lower())
        for line in out.splitlines():
            if line.startswith('- stars:'): self.assertIn('source', line); self.assertIn('as of', line)
        self.assertIn('Baseline 10 (source user typed', out)

    def test_report_inconclusive_says_not_enough_data(self):
        self.setup_campaign(); self.g('exp', 'start', 'e1'); self.t += 8 * DAY
        self.g('exp', 'decide', 'e1', '--decision', 'inconclusive', '--reason', 'too noisy')
        out = self.g('report')
        self.assertIn('Not enough data to decide', out); self.assertNotIn('moved with the change', out)

    def test_brief_has_context(self):
        self.setup_campaign(); self.queue()
        out = self.g('brief')
        for w in ('Goal: Grow stars', 'stars: 10', 'e1 [baselined]', 'q1 drafted', '@me', 'never "caused"', 'moved with'):
            self.assertIn(w, out)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(); self.addCleanup(self._tmp.cleanup); self.dir = Path(self._tmp.name)

    def g(self, *args, code=0, stdin=None, tty=False):
        if tty:  # a real terminal for stdin: posts can only be approved by a person at the keyboard
            import pty
            master, slave = pty.openpty()
            try:
                os.write(master, (stdin or '').encode())
                r = subprocess.run([sys.executable, GRAPH, *args], cwd=self.dir, capture_output=True, text=True, encoding='utf-8', stdin=slave)
            finally: os.close(master); os.close(slave)
        else: r = subprocess.run([sys.executable, GRAPH, *args], cwd=self.dir, capture_output=True, text=True, encoding='utf-8', input=stdin)
        self.assertEqual(r.returncode, code, f'{args}: {r.stdout}{r.stderr}')
        return r.stdout + r.stderr

    def post_it(self, qid, result, *extra, code=0):
        out = self.g('grow', 'post', qid, '--claim', '--rung', '1', *extra, code=code)
        if code: return
        tok = re.search(r'Token (\w+)', out).group(1)
        self.g('grow', 'post', qid, '--confirm', tok, '--result', result)

    def test_cli_tick_opens_run_and_checkpoint_go_approves_card(self):
        self.g('grow', 'init', 'Grow stars', '--id', 'c1', '--metric', 'stars')
        self.g('grow', 'source', 'add', 'stars', '--via', 'manual')
        self.g('grow', 'channel', 'add', 'x', '--handle', '@me')
        self.g('grow', 'read', '--metric', 'stars', '--value', '10', '--as-of', time_today())
        self.g('grow', 'exp', 'add', '--hypothesis', 'h', '--surface', 's', '--change-kind', 'post', '--change-ref', 'p', '--metric', 'stars')
        self.g('grow', 'exp', 'baseline', 'e1')
        out = self.g('grow', 'tick')
        self.assertIn('run c1-c1', out); self.assertTrue((self.dir / '.graph/runs/c1-c1/state.json').exists())
        self.assertEqual(json.loads((self.dir / '.graph/runs/c1-c1/state.json').read_text())['campaign'], 'c1')
        # an ordinary small run joined to the campaign, to try the checkpoint screen without the medium-job gates
        self.g('init', 'cycle', '--id', 'r2', '--size', 'small', '--skip-preflight', 't', '--skip-context', 't', '--skip-budget', 't')
        sp = self.dir / '.graph/runs/r2/state.json'; st = json.loads(sp.read_text()); st['campaign'] = 'c1'; sp.write_text(json.dumps(st))
        self.g('node', 'r2', 'draft', '--role', 'research', '--status', 'pending', '--goal', 'draft the post')
        (self.dir / '.graph/campaigns/c1/drafts').mkdir(parents=True, exist_ok=True)
        (self.dir / '.graph/campaigns/c1/drafts/a.md').write_text('My honest post', encoding='utf-8')
        self.g('grow', 'queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', 'drafts/a.md', '--rung', '1')
        self.g('checkpoint', '--run', 'r2', '--terminal', stdin='go\n', code=3)  # a pipe is not a person at the keyboard
        self.assertEqual(json.loads((self.dir / '.graph/campaigns/c1/queue.jsonl').read_text().splitlines()[-1])['state'], 'shown')
        shown = self.g('checkpoint', '--run', 'r2', '--terminal', stdin='go\n', tty=True)
        self.assertIn('My honest post', shown); self.assertIn('POST TO X', shown)
        q = [json.loads(l) for l in (self.dir / '.graph/campaigns/c1/queue.jsonl').read_text().splitlines()]
        self.assertEqual(q[-1]['state'], 'approved'); self.assertTrue(q[-1]['approved']['pressed-go'])
        self.post_it('q1', 'https://x.com/me/1')
        log = (self.dir / '.graph/campaigns/c1/log.jsonl').read_text()
        self.assertIn('"cmd": "post"', log)


    def test_cli_checkpoint_drop_card(self):
        self.g('grow', 'init', 'Grow stars', '--id', 'c1', '--metric', 'stars')
        self.g('grow', 'source', 'add', 'stars', '--via', 'manual')
        self.g('grow', 'channel', 'add', 'x', '--handle', '@me')
        self.g('grow', 'read', '--metric', 'stars', '--value', '10', '--as-of', time_today())
        self.g('grow', 'exp', 'add', '--hypothesis', 'h', '--surface', 's', '--change-kind', 'post', '--change-ref', 'p', '--metric', 'stars')
        self.g('grow', 'exp', 'baseline', 'e1')
        self.g('init', 'cycle', '--id', 'r2', '--size', 'small', '--skip-preflight', 't', '--skip-context', 't', '--skip-budget', 't')
        sp = self.dir / '.graph/runs/r2/state.json'; st = json.loads(sp.read_text()); st['campaign'] = 'c1'; sp.write_text(json.dumps(st))
        self.g('node', 'r2', 'draft', '--role', 'research', '--status', 'pending', '--goal', 'draft the post')
        (self.dir / '.graph/campaigns/c1/drafts').mkdir(parents=True, exist_ok=True)
        (self.dir / '.graph/campaigns/c1/drafts/a.md').write_text('My honest post', encoding='utf-8')
        self.g('grow', 'queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', 'drafts/a.md', '--rung', '1')
        self.g('checkpoint', '--run', 'r2', '--terminal', stdin='drop 2\n', tty=True)
        q = [json.loads(l) for l in (self.dir / '.graph/campaigns/c1/queue.jsonl').read_text().splitlines()]
        self.assertEqual(q[-1]['state'], 'dropped')
        self.post_it('q1', 'https://x.com/me/1', code=3)

    def test_http_go_without_shown_ids_approves_no_post_and_link_is_hidden(self):
        import urllib.request, time
        self.g('grow', 'init', 'Grow stars', '--id', 'c1', '--metric', 'stars')
        self.g('grow', 'source', 'add', 'stars', '--via', 'manual')
        self.g('grow', 'channel', 'add', 'x', '--handle', '@me')
        self.g('grow', 'read', '--metric', 'stars', '--value', '10', '--as-of', time_today())
        self.g('grow', 'exp', 'add', '--hypothesis', 'h', '--surface', 's', '--change-kind', 'post', '--change-ref', 'p', '--metric', 'stars')
        self.g('grow', 'exp', 'baseline', 'e1')
        self.g('init', 'cycle', '--id', 'r2', '--size', 'small', '--skip-preflight', 't', '--skip-context', 't', '--skip-budget', 't')
        sp = self.dir / '.graph/runs/r2/state.json'; st = json.loads(sp.read_text()); st['campaign'] = 'c1'; sp.write_text(json.dumps(st))
        self.g('node', 'r2', 'draft', '--role', 'research', '--status', 'pending', '--goal', 'draft the post')
        (self.dir / '.graph/campaigns/c1/drafts').mkdir(parents=True, exist_ok=True)
        (self.dir / '.graph/campaigns/c1/drafts/a.md').write_text('My honest post', encoding='utf-8')
        self.g('grow', 'queue', 'add', '--experiment', 'e1', '--channel', 'x', '--kind', 'post', '--draft', 'drafts/a.md', '--rung', '1')
        env = dict(os.environ, BROWSER='true')
        r = subprocess.run([sys.executable, GRAPH, 'checkpoint', '--run', 'r2', '--serve', '--background'], cwd=self.dir, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = json.loads((self.dir / '.graph/runs/r2/checkpoint.json').read_text())
        try:
            self.assertNotIn(st['url'], r.stdout)  # posts wait: the link is not printed to the model
            req = urllib.request.Request(st['url'].replace('/?', '/go?'), data=json.dumps({'dropped': []}).encode(), method='POST', headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 Chrome/120'})
            urllib.request.urlopen(req, timeout=10).read()
        finally:
            subprocess.run([sys.executable, GRAPH, 'checkpoint', '--run', 'r2', '--stop'], cwd=self.dir, capture_output=True)
        q = [json.loads(l) for l in (self.dir / '.graph/campaigns/c1/queue.jsonl').read_text().splitlines()]
        self.assertEqual(q[-1]['state'], 'shown')  # the page listed nothing, so nothing is approved


def time_today():
    import time
    return time.strftime('%Y-%m-%d', time.gmtime())


if __name__ == '__main__':
    unittest.main()
