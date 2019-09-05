"""Stratum V1 pool implementation

"""
from sim_primitives.pool import MiningSession, Pool
from .messages import *
from ..protocol import UpstreamConnectionProcessor
import enum


class MiningSessionV1(MiningSession):
    """V1 specific mining session registers authorize requests """

    class States(enum.Enum):
        """Stratum V1 mining session follows the state machine below."""

        INIT = 0
        # BIP310 configuration step
        CONFIGURED = 1
        AUTHORIZED = 2
        SUBSCRIBED = 3
        RUNNING = 4

    def __init__(self, *args, **kwargs):
        self.state = self.States.INIT
        super().__init__(*args, **kwargs)

        self.authorize_requests = []

    def run(self):
        """V1 Session switches its state"""
        super().run()
        self.state = self.States.RUNNING

    def append_authorize(self, msg: Authorize):
        self.authorize_requests.append(msg)


class PoolV1(UpstreamConnectionProcessor):
    """Processes all messages on 1 connection

    """
    def __init__(self, pool, connection):
        self.pool = pool
        self.__mining_session = pool.new_mining_session(connection, self._on_vardiff_change, clz=MiningSessionV1)
        super().__init__(pool.name, pool.env, pool.bus, connection)

    @property
    def mining_session(self):
        """Accessor for the current mining session cannot fail.

        """
        assert self.__mining_session is not None, 'Message processor has no mining session!'
        return self.__mining_session

    def terminate(self):
        super().terminate()
        self.__mining_session.terminate()

    def visit_subscribe(self, msg: Subscribe):
        """Handle mining.subscribe.
        """
        mining_session = self.__mining_session

        self.bus.emit(
            self.name,
            self.env.now,
            self.connection.uid,
            'SUBSCRIBE: {}'.format(mining_session.state),
            msg,
        )

        if mining_session.state in (
                mining_session.States.INIT,
                mining_session.States.AUTHORIZED,
        ):
            # Subscribe is now complete we can activate a mining session that starts
            # generating new jobs immediately
            mining_session.state = mining_session.States.SUBSCRIBED
            self._send_msg(
                SubscribeResponse(
                    msg.req_id,
                    subscription_ids=None,
                    # TODO: Extra nonce 1 is 8 bytes long and hardcoded
                    extranonce1=bytes([0] * 8),
                    extranonce2_size=self.pool.extranonce2_size,
                ),
            )
            # Run the session so that it starts supplying jobs
            mining_session.run()
        else:
            self._send_msg(
                ErrorResult(
                    msg.req_id,
                    -1,
                    'Subscribe not expected when in: {}'.format(mining_session.state),
                ),
            )

    def visit_authorize(self, msg: Authorize):
        """Parse authorize.
        Sending authorize is legal at any state of the mining session.
        """
        mining_session = self.__mining_session()
        mining_session.append_authorize(msg)
        self.bus.emit(
            self.name,
            self.env.now,
            self.connection.uid,
            'AUTHORIZE: {}'.format(mining_session.state),
            msg,
        )
        # TODO: Implement username validation and fail to authorize for unknown usernames
        self._send_msg(OkResult(msg.req_id))

    def visit_submit(self, msg: Submit):
        mining_session = self.__mining_session()
        self.bus.emit(
            self.name,
            self.env.now,
            self.connection.uid,
            'SUBMIT: {}'.format(mining_session.state),
            msg,
        )
        self.pool.process_submit(
            msg.job_id,
            mining_session,
            on_accept=lambda: self._send_msg(OkResult(msg.req_id)),
            on_reject=lambda: self._send_msg(ErrorResult(msg.req_id, -3, 'Too low difficulty')),
        )

    def on_new_block(self):
        self._send_msg(self.__build_mining_notify(clean_jobs=True))

    def _on_invalid_message(self, msg):
        self._send_msg(
            ErrorResult(msg.req_id, -2, 'Unrecognized message: {}'.format(msg)),
        )

    def _on_vardiff_change(self, session: MiningSession):
        """Handle difficulty change for the current session.

        Note that to enforce difficulty change as soon as possible,
        the message is accompanied by generating new mining job
        """
        self._send_msg(SetDifficulty(session.curr_diff))

        self._send_msg(self.__build_mining_notify(clean_jobs=False))

    def __build_mining_notify(self, clean_jobs: bool):
        """
        :param clean_jobs: flag that causes the client to flush all its jobs
        and immediately start mining on this job
        :return: MiningNotify message
        """
        session = self.mining_session
        if clean_jobs:
            session.job_registry.retire_all_jobs()
        job = session.job_registry.new_mining_job(diff_target=session.curr_target)

        return Notify(
            job_id=job.uid,
            prev_hash=self.pool.prev_hash,
            coin_base_1=None,
            coin_base_2=None,
            merkle_branch=None,
            version=None,
            bits=None,
            time=self.env.now,
            clean_jobs=clean_jobs,
        )
