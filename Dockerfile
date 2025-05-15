# syntax=docker/dockerfile:1.7-labs
# Start from a base image with Miniconda installed
FROM continuumio/miniconda3

# Install system dependencies
RUN apt-get update && \
    apt-get install -y sudo libusb-1.0 python3-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /backend-api

# Create the environment from the environment.yml file
COPY environment.yml ./
RUN conda env create -f environment.yml

# Make RUN commands use the new environment
SHELL ["conda", "run", "-n", "backend-api", "/bin/bash", "-c"]

# Copy Optional wheels directory and handle wheel installation (for utilizing local hummingbot version)
COPY --parents wheels* . 

# Optionally install local Hummingbot wheel if present - use the latest wheel only
RUN if [ -n "$(find wheels/ -name 'hummingbot-*.whl' 2>/dev/null)" ]; then \
    echo "Installing local Hummingbot wheel..." && \
    LATEST_WHEEL=$(find wheels/ -name 'hummingbot-*.whl' | sort -r | head -n1) && \
    echo "Using wheel: $LATEST_WHEEL" && \
    pip install --force-reinstall $LATEST_WHEEL && \
    echo "Local Hummingbot wheel installed successfully"; \
    else \
    echo "No local Hummingbot wheel found, using version from environment.yml"; \
    fi

# Copy the current directory contents and the Conda environment file into the container
COPY main.py models.py config.py LICENSE README.md ./
COPY utils/ utils/
COPY routers/ routers/
COPY services/ services/

COPY bots/controllers bots/credentials bots/scripts bots/__init__.py bots/

# Add any other specific directories or files needed

# The code to run when container is started
ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "backend-api", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
