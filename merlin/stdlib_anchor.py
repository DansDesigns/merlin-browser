"""Standard modules to carry in Merlin.exe, whether or not they are used yet.

Merlin.exe holds its own copy of Python's standard library, but PyInstaller
only includes the modules it sees being imported when the exe is built. The
merlin package is then loaded from disk and can be newer than the exe, so an
update that uses a standard module nothing used at build time fails on
Windows alone, with "No module named ...". That is how fetching Deno broke:
it was the first code to import platform.

This module names the standard library broadly. The imports sit in a block
that never runs, so nothing is loaded at start-up; PyInstaller reads imports
from the compiled code rather than by running it, so it bundles every one.
install.bat names this module with --hidden-import.

Left out: modules with no place in a browser (tkinter, turtle, the test suite,
the IDLE editor, and so on). A module that does not exist on the platform being
built for is simply skipped by PyInstaller with a warning.

Regenerate with tools/make-stdlib-anchor.py after moving to a new Python.
"""

if __name__ == "__merlin_never_runs__":
    import __future__  # noqa: F401
    import abc  # noqa: F401
    import aifc  # noqa: F401
    import argparse  # noqa: F401
    import array  # noqa: F401
    import ast  # noqa: F401
    import asyncio  # noqa: F401
    import atexit  # noqa: F401
    import audioop  # noqa: F401
    import base64  # noqa: F401
    import bdb  # noqa: F401
    import binascii  # noqa: F401
    import bisect  # noqa: F401
    import builtins  # noqa: F401
    import bz2  # noqa: F401
    import cProfile  # noqa: F401
    import calendar  # noqa: F401
    import cgi  # noqa: F401
    import cgitb  # noqa: F401
    import chunk  # noqa: F401
    import cmath  # noqa: F401
    import cmd  # noqa: F401
    import code  # noqa: F401
    import codecs  # noqa: F401
    import codeop  # noqa: F401
    import collections  # noqa: F401
    import colorsys  # noqa: F401
    import compileall  # noqa: F401
    import concurrent  # noqa: F401
    import concurrent.futures  # noqa: F401
    import configparser  # noqa: F401
    import contextlib  # noqa: F401
    import contextvars  # noqa: F401
    import copy  # noqa: F401
    import copyreg  # noqa: F401
    import csv  # noqa: F401
    import ctypes  # noqa: F401
    import dataclasses  # noqa: F401
    import datetime  # noqa: F401
    import decimal  # noqa: F401
    import difflib  # noqa: F401
    import dis  # noqa: F401
    import doctest  # noqa: F401
    import email  # noqa: F401
    import encodings  # noqa: F401
    import enum  # noqa: F401
    import errno  # noqa: F401
    import faulthandler  # noqa: F401
    import fcntl  # noqa: F401
    import filecmp  # noqa: F401
    import fileinput  # noqa: F401
    import fnmatch  # noqa: F401
    import fractions  # noqa: F401
    import ftplib  # noqa: F401
    import functools  # noqa: F401
    import gc  # noqa: F401
    import genericpath  # noqa: F401
    import getopt  # noqa: F401
    import getpass  # noqa: F401
    import gettext  # noqa: F401
    import glob  # noqa: F401
    import graphlib  # noqa: F401
    import grp  # noqa: F401
    import gzip  # noqa: F401
    import hashlib  # noqa: F401
    import heapq  # noqa: F401
    import hmac  # noqa: F401
    import html  # noqa: F401
    import html.parser  # noqa: F401
    import http  # noqa: F401
    import imaplib  # noqa: F401
    import imghdr  # noqa: F401
    import importlib  # noqa: F401
    import inspect  # noqa: F401
    import io  # noqa: F401
    import ipaddress  # noqa: F401
    import itertools  # noqa: F401
    import json  # noqa: F401
    import keyword  # noqa: F401
    import linecache  # noqa: F401
    import locale  # noqa: F401
    import logging  # noqa: F401
    import lzma  # noqa: F401
    import mailbox  # noqa: F401
    import mailcap  # noqa: F401
    import marshal  # noqa: F401
    import math  # noqa: F401
    import mimetypes  # noqa: F401
    import mmap  # noqa: F401
    import modulefinder  # noqa: F401
    import msvcrt  # noqa: F401
    import multiprocessing  # noqa: F401
    import netrc  # noqa: F401
    import nntplib  # noqa: F401
    import nt  # noqa: F401
    import ntpath  # noqa: F401
    import nturl2path  # noqa: F401
    import numbers  # noqa: F401
    import opcode  # noqa: F401
    import operator  # noqa: F401
    import optparse  # noqa: F401
    import os  # noqa: F401
    import pathlib  # noqa: F401
    import pdb  # noqa: F401
    import pickle  # noqa: F401
    import pickletools  # noqa: F401
    import pipes  # noqa: F401
    import pkgutil  # noqa: F401
    import platform  # noqa: F401
    import plistlib  # noqa: F401
    import poplib  # noqa: F401
    import posix  # noqa: F401
    import posixpath  # noqa: F401
    import pprint  # noqa: F401
    import profile  # noqa: F401
    import pstats  # noqa: F401
    import pwd  # noqa: F401
    import py_compile  # noqa: F401
    import pyclbr  # noqa: F401
    import pyexpat  # noqa: F401
    import queue  # noqa: F401
    import quopri  # noqa: F401
    import random  # noqa: F401
    import re  # noqa: F401
    import reprlib  # noqa: F401
    import resource  # noqa: F401
    import rlcompleter  # noqa: F401
    import runpy  # noqa: F401
    import sched  # noqa: F401
    import secrets  # noqa: F401
    import select  # noqa: F401
    import selectors  # noqa: F401
    import shelve  # noqa: F401
    import shlex  # noqa: F401
    import shutil  # noqa: F401
    import signal  # noqa: F401
    import site  # noqa: F401
    import smtplib  # noqa: F401
    import sndhdr  # noqa: F401
    import socket  # noqa: F401
    import socketserver  # noqa: F401
    import sqlite3  # noqa: F401
    import ssl  # noqa: F401
    import stat  # noqa: F401
    import statistics  # noqa: F401
    import string  # noqa: F401
    import stringprep  # noqa: F401
    import struct  # noqa: F401
    import subprocess  # noqa: F401
    import sunau  # noqa: F401
    import symtable  # noqa: F401
    import sys  # noqa: F401
    import sysconfig  # noqa: F401
    import syslog  # noqa: F401
    import tarfile  # noqa: F401
    import telnetlib  # noqa: F401
    import tempfile  # noqa: F401
    import termios  # noqa: F401
    import textwrap  # noqa: F401
    import threading  # noqa: F401
    import time  # noqa: F401
    import timeit  # noqa: F401
    import token  # noqa: F401
    import tokenize  # noqa: F401
    import tomllib  # noqa: F401
    import trace  # noqa: F401
    import traceback  # noqa: F401
    import tracemalloc  # noqa: F401
    import types  # noqa: F401
    import typing  # noqa: F401
    import unicodedata  # noqa: F401
    import unittest  # noqa: F401
    import urllib  # noqa: F401
    import urllib.error  # noqa: F401
    import urllib.parse  # noqa: F401
    import urllib.request  # noqa: F401
    import uu  # noqa: F401
    import uuid  # noqa: F401
    import warnings  # noqa: F401
    import wave  # noqa: F401
    import weakref  # noqa: F401
    import webbrowser  # noqa: F401
    import winreg  # noqa: F401
    import wsgiref  # noqa: F401
    import xdrlib  # noqa: F401
    import xml  # noqa: F401
    import xmlrpc  # noqa: F401
    import zipapp  # noqa: F401
    import zipfile  # noqa: F401
    import zipimport  # noqa: F401
    import zlib  # noqa: F401
    import zoneinfo  # noqa: F401
