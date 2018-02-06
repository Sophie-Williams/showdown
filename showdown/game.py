#!/usr/bin/env python
import enum
import errno
import io
import logging
import os
import queue
import random
import subprocess
import sys
import threading

logger = logging.getLogger(__file__)

STARTUP_TIME = 10
TURN_TIME = 1
TOTAL_TURNS = 100
MAX_BULLETS = 6


class Commands(enum.Enum):
    STAND = "stand"
    SHOOT = "shoot"
    DODGE = "dodge"
    RELOAD = "reload"
    SHOOT_NO_BULLET = "shoot_no_bullet"
    GAME_OVER = "game_over"


def enqueue_output(out, queue):
    for line in iter(out.readline, b''):
        queue.put(line)
    out.close()


class Contestant:
    def __init__(self, call_args):
        self.call_args = call_args
        self.exited = False
        self.start()
        if self.exited:
            return
        self.contestant_name = self.read_name()
        self.bullets = 1
        self.num_dodges = 0
        self.latest_command = None

    @property
    def name(self):
        return getattr(self, "contestant_name",
                       " ".join(self.call_args))

    def start(self):
        try:
            self.process = subprocess.Popen(
                self.call_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0, close_fds=True)
        except PermissionError:
            self.exited = True
            print(f"Command '{self.name}': Permission denied")
            return
        except FileNotFoundError:
            self.exited = True
            print(f"Command '{self.name}' not found")
            return

        self.stdout_queue = queue.Queue(maxsize=1 + TOTAL_TURNS)
        self.stdout_thread = threading.Thread(
            target=enqueue_output,
            args=(self.process.stdout, self.stdout_queue))

        # thread dies with the program
        self.stdout_thread.daemon = True
        self.stdout_thread.start()

    def read_name(self):
        try:
            name = self.read(timeout=STARTUP_TIME)

        except EOFError:
            self.exited = True
            print(f"{self.name} exited")
            return
        except TimeoutError:
            self.exited = True
            print(f"{self.name} took more than {STARTUP_TIME}"
                  " seconds to output its name")
            return

        if not name:
            print(f"{self.name} didn't send its name")
            self.exited = True

        return name

    def read(self, timeout):
        if self.process.poll() is not None:
            raise EOFError
        try:
            line = self.stdout_queue.get(timeout=timeout)
            return line.decode("utf-8").strip()
        except queue.Empty:
            raise TimeoutError

    def is_alive(self):
        return not self.exited and self.process.poll() is None

    def ask(self):
        command = Commands.STAND

        # Check if process is alive
        if not self.is_alive():
            logger.warning(
                f"{self.name} has terminated ({self.process.returncode})")
            return Commands.STAND

        # Read stdout, with a hard timeout
        try:
            command = self.read(timeout=TURN_TIME)
        except EOFError:
            logger.warning(
                f"{self.name} has exited.")
            return Commands.STAND
        except TimeoutError:
            logger.warning(
                f"{self.name} took more than {TURN_TIME} "
                f"seconds to answer.")
            return Commands.GAME_OVER

        # Check for valid command
        try:
            command = Commands(command.strip())
        except ValueError:
            logger.warning(f"{self.name} issued invalid command {command}")

            return Commands.STAND

        if command not in [Commands.SHOOT, Commands.DODGE, Commands.RELOAD]:
            logger.warning(
                f"{self.name} issued invalid command {command.value}")

            return Commands.STAND

        # Update internal state for shoot
        if command == Commands.SHOOT:
            if self.bullets:
                self.bullets -= 1
            else:
                logger.info(f"{self.name} shot but without bullets")

                return Commands.SHOOT_NO_BULLET

        # Update internal state for reload
        if command == Commands.RELOAD:
            if self.bullets == MAX_BULLETS:
                logger.info(f"{self.name} reloaded but already full")
            else:
                self.bullets += 1

        # Update internal state for reload
        if command == Commands.DODGE:
            self.num_dodges += 1

        logger.info(f"{self.name} command: {command.value}")
        return command

    def tell(self, command):
        self.process.stdin.write(
            command.value.encode("utf-8") + b"\n")
        self.process.stdin.flush()


    def kill(self):
        # TODO log stderr
        try:
            self.process.stdin.close()
            self.process.kill()
        except AttributeError:
            pass


def usage():
    print(f"Usage: {sys.argv[0]} program_a args -vs- program_b args")
    sys.exit(1)


def game(state_queue):
    state = {}
    try:
        state = setup()
        while True:
            cont = loop(state, state_queue)
            if not cont:
                finish(state, state_queue)
            write_to_ui_queue(state, state_queue)
            if not cont:
                break
    finally:
        clean(state)


def setup():
    state = {
        "num_turn": 0,
    }

    args = sys.argv[1:]
    try:
        index = args.index("-vs-")
    except ValueError:
        usage()

    call_args_a = args[:index]
    call_args_b = args[index + 1:]

    state["a"] = Contestant(call_args_a)
    state["b"] = Contestant(call_args_b)
    if any(not state[key].is_alive() for key in "ab"):
        sys.exit(1)
    setup_logging(state["a"].name, state["b"].name)
    return state


def setup_logging(name_a, name_b):
    hdlr = logging.FileHandler(f"{name_a}-vs-{name_b}.log")
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr)
    logger.setLevel(logging.DEBUG)
    logger.info(f"Starting new showdown: {name_a} vs {name_b}")


def loop(state, state_queue):
    logger.info(f"Turn {state['num_turn']} begins")
    command_a = state["a"].ask()
    state["a"].latest_command = command_a
    command_b = state["b"].ask()
    state["b"].latest_command = command_b
    if Commands.GAME_OVER in (command_a, command_b):
        winner = list("ab")
        if command_a == Commands.GAME_OVER:
            winner.remove("a")
        if command_b == Commands.GAME_OVER:
            winner.remove("b")
        winner_key = next(iter(winner), None)
        if winner_key:
            state["winner"] = state[winner_key]
        return False

    unprotected = [Commands.SHOOT_NO_BULLET, Commands.RELOAD, Commands.STAND]
    if command_a == Commands.SHOOT and command_b in unprotected:
        state["winner"] = state["a"]
        logger.info(f"{state['a'].name} shot {state['b'].name}")
        return False
    if command_b == Commands.SHOOT and command_a in unprotected:
        state["winner"] = state["b"]
        logger.info(f"{state['b'].name} shot {state['a'].name}")
        return False

    state["a"].tell(command_b)
    state["b"].tell(command_a)

    state["num_turn"] += 1

    if state["num_turn"] >= TOTAL_TURNS:
        return False

    return True

def write_to_ui_queue(state, state_queue):
    if "description" in state:
        description = state["description"]
    else:

        description = f"un truc au pif"

    new_state = {
        "num_turn": state["num_turn"],
        "description": description,
        "a": {
            "name": state["a"].name,
            "bullets": state["a"].bullets,
            "command": state["a"].latest_command.value,
            "num_dodges": state["a"].num_dodges
        },
        "b": {
            "name": state["b"].name,
            "bullets": state["b"].bullets,
            "command": state["b"].latest_command.value,
            "num_dodges": state["b"].num_dodges
        },
    }

    if "winner" in state:
        winner_key = "a" if state["winner"] is state["a"] else "b"
        new_state["winner_key"] = winner_key

    state_queue.put(new_state)


def finish(state, state_queue):
    winner = state.get('winner')
    description = ""
    if not winner:
        diff_dodges = state["a"].num_dodges - state["b"].num_dodges
        if diff_dodges:
            if diff_dodges < 0:
                winner = state["a"]
            elif diff_dodges > 0:
                winner = state["b"]
            description = (f"{winner.name} wins by having dodged "
                           f"{abs(diff_dodges)} times less")
        else:
            winner = random.choice([state["a"], state["b"]])
            description = f"Toss a coin: {winner.name} wins."
    else:
        description = f"{winner.name} wins"

    state['winner'] = winner
    state['description'] = description


def clean(state):
    try:
        state["a"].kill()
    except KeyError:
        pass
    try:
        state["b"].kill()
    except KeyError:
        pass
