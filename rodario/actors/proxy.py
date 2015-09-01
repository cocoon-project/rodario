""" Actor proxy for rodario framework """

# stdlib
import inspect
import types
import pickle
from multiprocessing import Queue
from threading import Thread
from uuid import uuid4
from time import sleep

# 3rd party
import redis


class ActorProxy(object):  # pylint: disable=I0011,R0903

    """ Proxy object that fires calls to an actor over redis pubsub """

    def __init__(self, actor=None, uuid=None):
        """
        Initialize instance of ActorProxy.

        Accepts either an Actor object to clone or a UUID, but not both.

        :param rodario.actors.Actor actor: Actor to clone
        :param str uuid: UUID of Actor to clone
        """

        #: Redis connection
        self._redis = redis.StrictRedis()
        #: Redis PubSub client
        self._pubsub = None
        #: This proxy object's UUID for creating unique channels
        self.proxyid = str(uuid4())
        #: Dict of response queues for sandboxing method calls
        self._response_queues = {}

        # avoid cyclic import
        actor_module = __import__('rodario.actors', fromlist=('Actor',))
        # pylint: disable=I0011,E1123
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        self._pubsub.subscribe(**{'proxy:%s' % self.proxyid: self._handler})

        methods = []

        def pubsub_thread():
            """ Call get_message in loop to fire _handler. """

            while True and self._pubsub:
                self._pubsub.get_message()
                sleep(0.01)

        # fire up the message handler thread as a daemon
        proc = Thread(target=pubsub_thread)
        proc.daemon = True
        proc.start()

        if isinstance(actor, actor_module.Actor):
            # proxying an Actor directly
            self.uuid = actor.uuid
            methods = inspect.getmembers(actor, predicate=inspect.ismethod)
        elif isinstance(uuid, str):
            # proxying by UUID; get actor methods over pubsub
            self.uuid = uuid
            pre_methods = self._proxy('_get_methods').get()

            for name in pre_methods:
                methods.append((name, None,))
        else:
            raise Exception('No actor or UUID provided')

        def get_lambda(name):
            """
            Generate a lambda function to proxy the given method.

            :param str name: Name of the method to proxy
            :rtype: :expression:`lambda`
            """

            return lambda _, *args, **kwargs: self._proxy(name, *args, **kwargs)

        # create proxy methods for each public method of the original Actor
        for name, _ in methods:
            if name[0] == '_':
                continue

            setattr(self, name, types.MethodType(get_lambda(name), self))

    def _handler(self, message):
        """
        Handle message response via Queue object.

        :param tuple message: The message to dissect
        """

        # throw its value in the associated response queue
        data = pickle.loads(message['data'])
        queue = data[0]
        self._response_queues[queue].put(data[1])
        self._response_queues.pop(queue, None)

    def _proxy(self, method_name, *args, **kwargs):
        """
        Proxy a method call to redis pubsub.

        :param str method_name: The method to proxy
        :param tuple args: The arguments to pass
        :param dict kwargs: The keyword arguments to pass
        :rtype: :class:`multiprocessing.Queue`
        """

        # create a unique response queue for retrieving the return value async
        queue = str(uuid4())
        # fire off the method call to the original Actor over pubsub
        count = self._redis.publish('actor:%s' % self.uuid,
                                    pickle.dumps((self.proxyid, queue,
                                                  method_name, args,
                                                  kwargs,)))

        if count == 0:
            raise Exception('No such actor')

        self._response_queues[queue] = Queue()

        return self._response_queues[queue]
