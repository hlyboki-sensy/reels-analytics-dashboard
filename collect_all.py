#!/usr/bin/env python3
"""Запускає обидва щоденні збирачі: сторіз + підписники. Для launchd."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_stories, collect_followers
collect_stories.main()
collect_followers.main()
