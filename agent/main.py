# Copyright (c) Microsoft. All rights reserved.
"""Five in-process agents behind one validated Responses endpoint."""

import logging

from learning_coach.server import create_server


def main():
    logger = logging.getLogger("learning_coach")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    create_server().run()


if __name__ == "__main__":
    main()
