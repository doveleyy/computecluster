"""Deliberately exceed a batch job's memory limit for an acceptance test."""

chunks: list[bytearray] = []
while True:
    chunks.append(bytearray(32 * 1024 * 1024))
