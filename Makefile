.PHONY: test demo package

test:
	PYTHONPATH=src python -m unittest discover -s tests -v

demo:
	./scripts/run_demo.sh

package:
	python -m build
