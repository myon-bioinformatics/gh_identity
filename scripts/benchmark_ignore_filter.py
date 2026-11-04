"""Isolated synthetic filtering comparison; no GitHub requests."""
import json
import statistics
import sys
import time


def baseline(checks,ignore):
    return [c for c in checks if c['name'] not in set(ignore)]


def indexed(checks,ignore):
    ignored=set(ignore)
    return [c for c in checks if c['name'] not in ignored]


def main():
    checks=[{'name':str(i)} for i in range(5000)]
    ignore=[str(i) for i in range(1000)]
    expected=baseline(checks,ignore)
    samples={'baseline':[],'indexed':[]}
    for repeat in range(8):
        for name in (list(samples) if repeat%2 else list(reversed(samples))):
            start=time.perf_counter_ns()
            result=(baseline if name=='baseline' else indexed)(checks,ignore)
            elapsed=time.perf_counter_ns()-start
            assert result==expected
            if repeat:samples[name].append(elapsed)
    print(json.dumps({'python':sys.version,'checks':5000,'ignored_names':1000,'samples_ns':samples,
                     'median_ns':{k:statistics.median(v) for k,v in samples.items()}},indent=2))


if __name__=='__main__':main()
