.PHONY: test demo serve

test:
	python -m unittest discover -s tests -v

demo:
	python examples/token_budget_demo.py

serve:
	python -m companion_memory serve
