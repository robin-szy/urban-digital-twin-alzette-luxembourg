"""Area-of-interest module for the Alzette digital twin.

A *reach* - a river with a start and end kilometre - is the project's single
AOI identity. It is one-dimensional: a validated centerline with chainage.
Every AOI polygon is derived from a reach by a named *variant*, and each
dataset is bound to the variant it must be processed against.
"""
