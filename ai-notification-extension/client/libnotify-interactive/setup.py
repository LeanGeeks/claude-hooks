from setuptools import setup, find_packages

setup(
    name="notify-interactive",
    version="0.1.0",
    description="Interactive desktop notifications with action buttons",
    author="Your Name",
    author_email="your.email@example.com",
    url="https://github.com/yourusername/ai-notification-extension",
    packages=find_packages(),
    install_requires=[
        "PyGObject>=3.42.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
            "mypy>=1.0.0",
        ],
    },
    python_requires=">=3.8",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)
