# kg_obo

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=Knowledge-Graph-Hub_kg-obo&metric=alert_status)](https://sonarcloud.io/dashboard?id=Knowledge-Graph-Hub_kg-obo)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=Knowledge-Graph-Hub_kg-obo&metric=sqale_rating)](https://sonarcloud.io/dashboard?id=Knowledge-Graph-Hub_kg-obo)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=Knowledge-Graph-Hub_kg-obo&metric=coverage)](https://sonarcloud.io/dashboard?id=Knowledge-Graph-Hub_kg-obo)

A package to transform all [OBO ontologies](http://obofoundry.org/) into [KGX TSV format](https://github.com/biolink/kgx/blob/master/specification/kgx-format.md), and put the transformed graphs in [KGhub](http://kg-hub.berkeleybop.io/index.html)

Installation:
```
git clone https://github.com/Knowledge-Graph-Hub/kg-obo.git
cd kg-obo
python -m venv venv # optional
pip install .
```

Usage:
```
python run.py
```
