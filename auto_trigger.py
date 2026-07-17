import json, subprocess, sys, time

try:
    d = json.load(open('completed_links.json'))
except Exception:
    d = {'folders': {}}

folders = d.get('folders', {})
all_done = True
remaining = 0

for name, f in folders.items():
    total = f.get('total', 0)
    done = f.get('done', 0)
    status = f.get('status', 'pending')
    if status != 'completed' or done < total:
        all_done = False
        remaining += total - done
        print(f'  [{name}] {done}/{total} ({status})', file=sys.stderr)

if remaining > 0:
    max_attempts = 3
    for attempt in range(max_attempts):
        r = subprocess.run(
            ['gh', 'workflow', 'run', 'MEGA to Google Drive Transfer', '--ref', 'main'],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            print(f'Triggered next cycle ({remaining} files remaining)')
            break
        print(f'Attempt {attempt+1}/{max_attempts} failed: {r.stderr.strip()}')
        if attempt < max_attempts - 1:
            time.sleep(10)
    else:
        print('All trigger attempts failed')
        sys.exit(1)
elif folders and all_done:
    print('All folders completed — no more cycles')
else:
    print('No pending work — no more cycles')
