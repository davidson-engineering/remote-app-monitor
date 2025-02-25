#!/usr/bin/env python
# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Created By  : Matthew Davidson
# Created Date: 2024-01-01
# version ='0.0.1'
# ---------------------------------------------------------------------------
"""a_short_project_description"""
# ---------------------------------------------------------------------------

import logging
from logging.config import dictConfig
import asyncio
import math

# Import the load_configs function
from config_loader import load_configs

from app_monitor import (
    TerminalManager,
    ProgressBar,
    Table,
    RangeBar,
    TextElement,
    ZeroMQUpdateServer,
    SerialUpdateServer,
    LogMonitor,
)
from app_monitor.elements_advanced import CoordinateTextElement
from app_monitor.server import OrderedDecoder

LOGGING_CONFIG_FILEPATH = "config/logging.yaml"
APP_CONFIG_FILEPATH = "config/application.toml"

# Load user configurations using the config_loader module
configs = load_configs([APP_CONFIG_FILEPATH, LOGGING_CONFIG_FILEPATH])

# Configure logging using the specified logging configuration
dictConfig(configs["logging"])


async def main():
    logging.info(configs["application"])

    # Create a MonitorManager instances
    manager = TerminalManager()

    # Add a progress bar and table elements with formatting
    BAR_WIDTH = 30
    text_format = {"bold": True}
    position = CoordinateTextElement(
        element_id="position", text="Position", text_format=text_format, units="mm"
    )

    manager.add_element(position)

    # Example instantiation of the RangeBar
    axis_properties = dict(
        min_value=-10,
        max_value=10,
        # width=50,  # Total width of the bar (e.g., 50 characters)
        bar_format={"fg_color": "green"},  # Custom formatting for the bar (optional)
        text_format={"bold": True},  # Custom formatting for the display text (optional)
        max_label_length=12,  # Maximum length of the label (e.g., 5 characters)
        max_display_length=5,  # Maximum length of the value (e.g., 5 characters)
        marker_trace="█",
        range_trace="-",
        digits=1,
        scale=60 / 6000,
    )
    axis_velocity = RangeBar(
        element_id="velocity", label="Axis Vel", unit="mm/s", **axis_properties
    )
    axis_torque = RangeBar(
        element_id="torque", label="Axis Torque", unit="Nm", **axis_properties
    )

    logger = LogMonitor(
        element_id="logger",
        timestamp=True,
        timestamp_format="%H:%M:%S.%f",
        # timestamp_significant_digits=3,
        border=True,
    )

    axis_group = [axis_velocity, axis_torque]
    manager.add_element_group("X", axis_group)
    manager.add_element_group("Y", axis_group)
    manager.add_element_group("Z", axis_group)

    manager.add_element(logger)

    # Start serial manager and subscriber
    server = SerialUpdateServer(
        manager,
        port="/dev/tty.usbserial-1440",
        baudrate=115200,
        decoder=OrderedDecoder(keys=["X.velocity", "Y.velocity", "Z.velocity"]),
    )

    # Create the task to update the monitor manager at a fixed rate
    asyncio.create_task(
        manager.update_fixed_rate(frequency=60)
    )  # Updates every 1 second

    # Start the ZeroMQ subscriber (it will process updates asynchronously)
    await server.start(frequency=60)


if __name__ == "__main__":
    asyncio.run(main())
