web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --access-logformat '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"' --forwarded-allow-ips='*'
