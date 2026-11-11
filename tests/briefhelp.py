"""Shared test helper: a valid field brief, so older tests can still approve medium and large runs."""
BRIEF = ['--field', 'test field', '--sources', 'primary docs', '--fresh', 'last two years', '--windows', 'paper=730,news=30,doc=any',
         '--landmark-ok', 'yes', '--evidence', 'two sources per claim', '--safety', 'Not professional advice.', '--review', 'a human expert']

def brief_cmd(rid='r1'): return ['brief', '--run', rid, *BRIEF]

def init_run_id(args):
    """The run id of an init call that makes a non-small run, else None."""
    if not args or args[0] != 'init': return None
    a = list(args)
    if '--size' in a and a[a.index('--size') + 1] == 'small': return None
    return a[a.index('--id') + 1] if '--id' in a else None
