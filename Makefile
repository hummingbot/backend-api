.ONESHELL:
.PHONY: run
.PHONY: uninstall
.PHONY: install

run:
	uvicorn main:app --reload

uninstall:
	conda env remove -n backend-api

install:
	conda env create -f environment.yml

docker_build:
	docker build -t hummingbot/backend-api:latest .

docker_run:
	docker compose up -d