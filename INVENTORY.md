# FantraxAPI Repository Inventory

This document provides a detailed inventory of all files and directories in the FantraxAPI repository, explaining their purpose and function.

## Root Directory Files

- `setup.py` - Python package configuration file that contains metadata and dependencies for the package installation
- `requirements.txt` - Lists the main project dependencies required to run the FantraxAPI
- `requirements-dev.txt` - Additional dependencies needed for development (testing, documentation, etc.)
- `VERSION` - Contains the current version number of the FantraxAPI package
- `LICENSE` - The license file specifying the terms under which the code can be used
- `README.rst` - The main documentation file in reStructuredText format, providing an overview of the project
- `test.py` - A standalone test script for quick testing purposes
- `cokkie.cook` - Configuration file (possibly for cookie handling or session management)

## Main Package Directory (`fantraxapi/`)

- `__init__.py` - Initializes the fantraxapi package and defines what gets exported
- `fantrax.py` - Core implementation of the Fantrax API client
- `exceptions.py` - Custom exception classes for the API
- `objs.py` - Object definitions and data models used by the API

## Tests Directory (`tests/`)

- `test_api.py` - Unit tests for the Fantrax API implementation

## Documentation Directory (`docs/`)

The `docs/` directory contains Sphinx documentation files:

- `conf.py` - Sphinx configuration file that controls the documentation build
- `index.rst` - Main documentation index file
- `intro.rst` - Introduction to the FantraxAPI
- `fantrax.rst` - Documentation for the main Fantrax API functionality
- `objs.rst` - Documentation for the API objects and data models
- `toc.rst` - Table of contents configuration
- `requirements.txt` - Dependencies required to build the documentation
- `Makefile` - Used to build the documentation on Unix-like systems
- `make.bat` - Used to build the documentation on Windows systems

## Directory Structure

```
FantraxAPI/
├── docs/               # Documentation files
├── fantraxapi/        # Main package source code
├── tests/             # Test files
└── Various config     # Configuration and setup files in root
```

This repository appears to be a Python package that provides an API client for interacting with Fantrax, likely a fantasy sports platform. The codebase is well-organized with separate directories for source code, tests, and documentation.
