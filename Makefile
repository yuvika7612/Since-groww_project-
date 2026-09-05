.PHONY: setup test demo dev seed lint

setup:
	cd backend && pip install -r requirements.txt
	cd frontend && npm install

test:
	cd backend && pytest -q

demo:
	cd backend && uvicorn app.main:app --reload

dev:
	cd frontend && npm run dev

seed:
	cd backend && python scripts/seed.py && python scripts/record_fixture.py

# compileall rather than a py_compile glob: "app/**/*.py" is only expanded by
# a shell with globstar on, and make uses /bin/sh, so the glob would pass
# through literally and the target would silently check nothing.
lint:
	cd backend && python -m compileall -q app workers scripts
	cd frontend && npm run build
