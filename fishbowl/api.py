from __future__ import unicode_literals
import base64
import socket
import struct
import hashlib
import functools
import logging
from lxml import etree

from . import xmlrequests, statuscodes, objects

logger = logging.getLogger(__name__)


class FishbowlError(Exception):
    pass


def require_connected(func):
    """
    A decorator to wrap :cls:`Fishbowl` methods that can only be called after a
    connection to the API server has been made.
    """

    @functools.wraps(func)
    def dec(self, *args, **kwargs):
        if not self.connected:
            raise OSError('Not connected')
        return func(self, *args, **kwargs)

    return dec


class Fishbowl:
    """
    Fishbowl API.

    Example usage::

        fishbowl = Fishbowl()
        fishbowl.connect(username='admin', password='admin')
    """
    host = 'localhost'
    port = 28192
    encoding = 'latin-1'

    def __init__(self):
        self._connected = False

    @property
    def connected(self):
        return self._connected

    def make_stream(self, timeout=5):
        """
        Create a connection to communicate with the API.
        """
        stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        logger.info('Connecting to {}:{}'.format(self.host, self.port))
        stream.connect((self.host, self.port))
        stream.settimeout(timeout)
        return stream

    def connect(self, username, password, host=None, port=None, timeout=5):
        """
        Open socket stream, set timeout, and log in.
        """
        password = base64.b64encode(
            hashlib.md5(password.encode(self.encoding)).digest())

        if self.connected:
            self.close()

        if host:
            self.host = host
        if port:
            self.port = int(port)
        self.stream = self.make_stream(timeout=float(timeout))
        self._connected = True

        try:
            self.key = None
            login_xml = xmlrequests.Login(username, password).request
            response = self.send_message(login_xml)
            # parse xml, grab api key, check status
            for element in response.iter():
                if element.tag == 'Key':
                    self.key = element.text
                if element.tag in ('loginRs', 'LoginRs', 'FbiMsgsRs'):
                    status_code = element.get('statusCode')
                    if status_code:
                        check_status(status_code)

            if not self.key:
                raise FishbowlError('No login key in response')
        except Exception:
            self.close(skip_errors=True)
            raise

    @require_connected
    def close(self, skip_errors=False):
        """
        Close connection to Fishbowl API.
        """
        self._connected = False
        self.key = None
        try:
            self.stream.close()
        except Exception:
            if not skip_errors:
                raise

    def pack_message(self, msg):
        """
        Calculate msg length and prepend to msg.
        """
        msg_length = len(msg)
        # '>L' = 4 byte unsigned long, big endian format
        packed_length = struct.pack('>L', msg_length)
        return packed_length + msg

    @require_connected
    def send_request(self, name, value=None):
        """
        Send a simple request to the API that follows the standard method.

        :param name: The syntax of the base XML node for the request
        :param value: A string containing the text of the base node, or a
            dictionary mapping to children nodes and their values
        """
        request = xmlrequests.SimpleRequest(name, value, key=self.key)
        return self.send_message(request)

    @require_connected
    def send_message(self, msg):
        """
        Send a message to the API and return the root element of the XML that
        comes back as a response.

        For higher level usage, see :meth:`send_request`.
        """
        if isinstance(msg, xmlrequests.Request):
            msg = msg.request

        tag = 'unknown'
        try:
            xml = etree.fromstring(msg)
            request_tag = xml.find('FbiMsgsRq')
            if request_tag is not None and len(request_tag):
                tag = request_tag[0].tag
        except etree.XMLSyntaxError:
            pass
        logger.info('Sending message ({})'.format(tag))
        logger.debug('Sending message:\n' + msg)
        self.stream.send(self.pack_message(msg))

        # Get response
        byte_count = 0
        response = b''
        received_length = False
        try:
            packed_length = self.stream.recv(4)
            length = struct.unpack('>L', packed_length)[0]
            received_length = True
            while byte_count < length:
                byte = self.stream.recv(1)
                byte_count += 1
                try:
                    response += byte.to_bytes(1, 'big')
                except AttributeError:   # Python 2
                    response += bytes(byte)
        except socket.timeout:
            self.close(skip_errors=True)
            if received_length:
                msg = 'Connection timeout (after length received)'
            else:
                msg = 'Connection timeout'
            raise FishbowlError(msg)
        response = response.decode(self.encoding)
        logger.debug('Response received:\n' + response)
        return etree.fromstring(response)

    def get_objects(self, response, response_node_name, object, node_name):
        obj_list = []
        base = response.find('FbiMsgsRs').find(response_node_name)
        for node in base.iter(node_name):
            obj_list.append(object(root_el=node))
        return obj_list

    @require_connected
    def add_inventory(self, partnum, qty, uomid, cost, loctagnum):
        """
        Add inventory.
        """
        request = xmlrequests.AddInventory(
            partnum, qty, uomid, cost, loctagnum, key=self.key)
        response = self.send_message(request)
        for element in response.iter('AddInventoryRs'):
            status_code = element.get('statusCode')
            if status_code:
                check_status(status_code)
            logger.info(','.join([
                '{}'.format(val)
                for val in ['add_inv', partnum, qty, uomid, cost, loctagnum]]))

    @require_connected
    def cycle_inventory(self, partnum, qty, locationid):
        """
        Cycle inventory of part in Fishbowl.
        """
        request = xmlrequests.CycleCount(
            partnum, qty, locationid, key=self.key)
        response = self.send_message(request)
        for element in response.iter('CycleCountRs'):
            status_code = element.get('statusCode')
            if status_code:
                check_status(status_code)
            logger.info(','.join([
                '{}'.format(val)
                for val in ['cycle_inv', partnum, qty, locationid]]))

    @require_connected
    def get_po_list(self, locationgroup):
        """
        Get list of POs.
        """
        request = xmlrequests.GetPOList(locationgroup, key=self.key)
        return self.send_message(request)

    @require_connected
    def get_customers(self):
        """
        Get customers.

        :returns: A list of lazy :cls:`fishbowl.objects.Customer` objects
        """
        customers = []
        request = self.send_request('CustomerNameListRq')
        for tag in request.find('FbiMsgsRs').iter('Name'):

            def lazy_customer():
                customer_req = self.send_request(
                    'CustomerGetRq', {'Name': tag.text})
                root = customer_req.find('FbiMsgsRs')
                return root.find('CustomerGetRs')[0]

            customer = objects.Customer(
                lazy_root_el=lazy_customer, name=tag.text)
            customers.append(customer)
        return customers

    @require_connected
    def get_vendors(self):
        """
        Get a list of vendors.

        :returns: A list of vendor names
        """
        customers = []
        request = self.send_request('VendorNameListRq')
        for tag in request.find('FbiMsgsRs').iter('Name'):

            def lazy_customer():
                customer_req = self.send_request(
                    'CustomerGetRq', {'Name': tag.text})
                root = customer_req.find('FbiMsgsRs')
                return root.find('CustomerGetRs')[0]

            customer = objects.Customer(
                lazy_root_el=lazy_customer, name=tag.text)
            customers.append(customer)
        return customers

    @require_connected
    def get_parts(self, populate_uoms=True):
        """
        Get a light list of parts.

        :param populate_uoms: Whether to populate the UOM for each part
            (default ``True``)
        :returns: A list of cls:`fishbowl.objects.Part`
        """
        response = self.send_request('LightPartListRq')
        parts = self.get_objects(
            response, 'LightPartListRs', objects.Part, 'LightPart')
        if populate_uoms:
            response = self.send_request('UOMRq')
            uom_map = dict(
                (uom['UOMID'], uom) for uom in
                self.get_objects(response, 'UOMRs', objects.UOM, 'UOM'))
            for part in parts:
                uomid = part.get('UOMID')
                if not uomid:
                    continue
                uom = uom_map.get(uomid)
                if uom:
                    part.mapped['UOM'] = uom
        return parts

    @require_connected
    def get_products(self, lazy=True):
        """
        Get a list of products, optionally lazy.

        The tricky thing is that there's no direct API for a product list, so
        we have to get a list of parts and then find the matching products.
        Understandably then, the non-lazy option is intensive, while the lazy
        option results in some products potentially being empty.

        :param lazy: Whether the products should be lazily loaded (default
            ``True``)
        :returns: A list of cls:`fishbowl.objects.Product`
        """
        products = []
        for part in self.get_parts(populate_uoms=False):

            def lazy_product():
                inner_req = self.send_request(
                    'ProductGetRq', {'Number': part['Number']})
                root = inner_req.find('FbiMsgsRs').find('ProductGetRs')
                if root.get('statusCode') == statuscodes.SUCCESS:
                    return root[0]
                # Need to return an element, an empty one is probably more
                # correct but the ProductGetRs root node is good enough.
                return root

            product_kwargs = {
                'name': part['Number'],
            }
            if lazy:
                product_kwargs['lazy_root_el'] = lazy_product
            else:
                request = self.send_request(
                    'ProductGetRq', {'Number': part['Number']})
                root = request.find('FbiMsgsRs').find('ProductGetRs')
                if root.get('statusCode') != statuscodes.SUCCESS:
                    continue
                product_kwargs['root_el'] = root[0]
            product = objects.Product(**product_kwargs)
            products.append(product)
        return products


def check_status(code, expected=statuscodes.SUCCESS):
    """
    Check a status code, raising an exception if it wasn't the expected code.
    """
    message = statuscodes.get_status(code)
    if code != expected:
        raise FishbowlError(message)
    return message
