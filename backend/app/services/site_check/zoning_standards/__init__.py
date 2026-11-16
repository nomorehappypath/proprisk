"""
core/zoning_standards/

Curated jurisdiction zoning rules engine. Per-city YAML files
under `curated/` capture permitted uses + dimensional standards
+ overlay interactions for every district that brokers,
investors, and lawyers actually need to underwrite from.

Engine reads the YAML at runtime, returns verified_public_fact
confidence with the source code section as the citation.
"""
