#!/usr/bin/env python3
"""Setup script for data-scripts CLI."""

from setuptools import setup, find_packages

setup(
    name="data-scripts",
    version="0.1.0",
    description="Data extraction and engineering intelligence toolkit for GitHub, PagerDuty, JIRA, ITSM, Dynatrace, and Azure APIs",
    author="Foods Platform & Architecture Practice",
    author_email="foods-engineering@mnscorp.net",
    license="AGPL-3.0-or-later",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.10",
    install_requires=[
        "click>=8.1.0",
        "requests>=2.31.0",
        "rich>=13.0.0",
        "tabulate>=0.9.0",
        "python-dateutil>=2.8.0",
        "pyyaml>=6.0",
        "matplotlib>=3.8.0",
        "numpy>=1.24.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "ruff>=0.4.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "data-scripts=datascripts.cli:cli",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
