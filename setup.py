# -*- coding: utf-8 -*-

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

readme = ""

setup(
    long_description=readme,
    name="violet",
    version="0.3.0",
    description="An asyncio background job library",
    python_requires="==3.*,>=3.7.0",
    author="elixi.re",
    author_email="python@elixi.re",
    license="LGPL-3.0",
    packages=["violet"],
    package_dir={"": "."},
    package_data={"violet": ["py.typed"]},
    install_requires=[
        "asyncpg>0.20",
        "hail @ git+https://gitlab.com/elixire/hail.git@d481786d256e682f6992ca250e9f8516205d0608#egg=hail",
    ],
)
