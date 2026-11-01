import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
HERE = os.path.dirname(os.path.abspath(__file__))
import random, time, hashlib
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, 'tools'))
import mixz, corpora, build_primers as bp
from concurrent.futures import ThreadPoolExecutor
mixz.USE_SNAPSHOTS = True
kind = 'py'
train, test = corpora.split(kind)
trainh = {hashlib.sha1(corpora.content(kind, f)).digest() for f in train}
rng = random.Random(3); rng.shuffle(test)
tests = []
for f in test:
    d = corpora.content(kind, f)
    if len(d) < 200 or hashlib.sha1(d).digest() in trainh: continue
    tests.append(d[:262144])
    if len(tests) >= 40: break
total = sum(map(len, tests))
for budget in (512 * 1024, 2048 * 1024, 8192 * 1024):
    docs = bp.load_train(kind, budget + 16384, 16384)
    pr = bp.primer_concat(docs, budget)
    # register as a temporary primer id so the snapshot path is used
    mixz._primers[1] = pr; mixz._snaps.clear()
    t = time.time()
    with ThreadPoolExecutor(2) as ex:
        sizes = list(ex.map(lambda d: len(mixz.cm_encode(d, mixz.P_TEXT, 1, 0, 1, 22) or d), tests))
    print(f"py primer concat {budget//1024:5d} KB: {sum(sizes)/total:.2%}  ({time.time()-t:.0f}s incl. priming)", flush=True)
