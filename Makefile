.PHONY: test lint dashboard render influxql

test:
	python3 -m unittest discover -s tests -v

lint:
	shellcheck -S warning install.sh uninstall.sh
	python3 -m pyflakes collector dhctl.py grafana tests

dashboard:
	python3 grafana/build_dashboard.py

render:
	./install.sh -c $(or $(CONFIG),config.env.example) --render-only ./out

influxql:
	cd tests/influxql_check && GOFLAGS=-mod=mod go build -o ../../.influxql_check .
	python3 -c "import json;d=json.load(open('grafana/disk-health.json'));q=[];\
	w=lambda ps:[(q.extend(t['query'] for t in p.get('targets',[])),w(p.get('panels',[]))) for p in ps];\
	w(d['panels']);q+=[v['query'] for v in d['templating']['list'] if v['type']=='query'];print(json.dumps(q))" \
	| ./.influxql_check
