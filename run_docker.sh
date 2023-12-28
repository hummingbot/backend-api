#!/bin/bash

# Generate .env file with the current working directory
echo "BOTS_PATH=$(pwd)" > .env

# Run Docker Compose
docker compose up
