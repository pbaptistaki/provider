import io
import json
import logging
import mimetypes
import os
import time
from cgi import parse_header

from flask import Response
from osmosis_driver_interface.osmosis import Osmosis
from web3.exceptions import BlockNumberOutofRange
from ocean_utils.agreements.service_agreement import ServiceAgreement
from ocean_utils.agreements.service_types import ServiceTypes

from ocean_provider.web3_internal.utils import add_ethereum_prefix_and_hash_msg
from ocean_provider.web3_internal.web3helper import Web3Helper
from ocean_provider.constants import BaseURLs
from ocean_provider.contracts.datatoken import DataTokenContract
from ocean_provider.exceptions import BadRequestError
from ocean_provider.utils.accounts import verify_signature, get_provider_account
from ocean_provider.utils.basics import get_config
from ocean_provider.utils.data_token import get_asset_for_data_token
from ocean_provider.utils.encryption import do_decrypt
from ocean_provider.utils.web3 import web3

logger = logging.getLogger(__name__)


def get_request_data(request, url_params_only=False):
    if url_params_only:
        return request.args
    return request.args if request.args else request.json


def build_download_response(request, requests_session, url, download_url, content_type=None):
    try:
        download_request_headers = {}
        download_response_headers = {}

        is_range_request = bool(request.range)

        if is_range_request:
            download_request_headers = {"Range": request.headers.get('range')}
            download_response_headers = download_request_headers

        response = requests_session.get(download_url, headers=download_request_headers, stream=True)

        if not is_range_request:
            filename = url.split("/")[-1]

            content_disposition_header = response.headers.get('content-disposition')
            if content_disposition_header:
                _, content_disposition_params = parse_header(content_disposition_header)
                content_filename = content_disposition_params.get('filename')
                if content_filename:
                    filename = content_filename

            content_type_header = response.headers.get('content-type')
            if content_type_header:
                content_type = content_type_header

            file_ext = os.path.splitext(filename)[1]
            if file_ext and not content_type:
                content_type = mimetypes.guess_type(filename)[0]
            elif not file_ext and content_type:
                # add an extension to filename based on the content_type
                extension = mimetypes.guess_extension(content_type)
                if extension:
                    filename = filename + extension

            download_response_headers = {
                "Content-Disposition": f'attachment;filename={filename}',
                "Access-Control-Expose-Headers": f'Content-Disposition'
            }

        return Response(
            io.BytesIO(response.content).read(),
            response.status_code,
            headers=download_response_headers,
            content_type=content_type
        )
    except Exception as e:
        logger.error(f'Error preparing file download response: {str(e)}')
        raise


def get_asset_files_list(asset, account):
    try:
        encrypted_files = asset.encrypted_files
        if encrypted_files.startswith('{'):
            encrypted_files = json.loads(encrypted_files)['encryptedDocument']
        files_str = do_decrypt(
            encrypted_files,
            account,
        )
        logger.debug(f'Got decrypted files str {files_str}')
        files_list = json.loads(files_str)
        if not isinstance(files_list, list):
            raise TypeError(f'Expected a files list, got {type(files_list)}.')

        return files_list
    except Exception as e:
        logger.error(f'Error decrypting asset files for asset {asset.did}: {str(e)}')
        raise


def get_asset_url_at_index(url_index, asset, account):
    logger.debug(f'get_asset_url_at_index(): url_index={url_index}, did={asset.did}, provider={account.address}')
    try:
        files_list = get_asset_urls(asset, account)
        if url_index >= len(files_list):
            raise ValueError(f'url index "{url_index}"" is invalid.')
        return files_list[url_index]

    except Exception as e:
        logger.error(f'Error decrypting url at index {url_index} for asset {asset.did}: {str(e)}')
        raise


def get_asset_urls(asset, account):
    logger.debug(f'get_asset_urls(): did={asset.did}, provider={account.address}')
    try:
        files_list = get_asset_files_list(asset, account)
        input_urls = []
        for i, file_meta_dict in enumerate(files_list):
            if not file_meta_dict or not isinstance(file_meta_dict, dict):
                raise TypeError(f'Invalid file meta at index {i}, expected a dict, got a '
                                f'{type(file_meta_dict)}.')
            if 'url' not in file_meta_dict:
                raise ValueError(f'The "url" key is not found in the '
                                 f'file dict {file_meta_dict} at index {i}.')

            input_urls.append(file_meta_dict['url'])

        return input_urls
    except Exception as e:
        logger.error(f'Error decrypting urls for asset {asset.did}: {str(e)}')
        raise


def get_asset_download_urls(asset, account, config_file):
    return [get_download_url(url, config_file)
            for url in get_asset_urls(asset, account)]


def get_download_url(url, config_file):
    try:
        logger.info('Connecting through Osmosis to generate the signed url.')
        osm = Osmosis(url, config_file)
        download_url = osm.data_plugin.generate_url(url)
        logger.debug(f'Osmosis generated the url: {download_url}')
        return download_url
    except Exception as e:
        logger.error(f'Error generating url (using Osmosis): {str(e)}')
        raise


def get_compute_endpoint():
    return get_config().operator_service_url + '/api/v1/operator/compute'


def check_required_attributes(required_attributes, data, method):
    assert isinstance(data, dict), 'invalid payload format.'
    logger.info('got %s request: %s' % (method, data))
    if not data:
        logger.error('%s request failed: data is empty.' % method)
        return 'payload seems empty.', 400
    for attr in required_attributes:
        if attr not in data:
            logger.error('%s request failed: required attr %s missing.' % (method, attr))
            return '"%s" is required in the call to %s' % (attr, method), 400
    return None, None


def validate_token_transfer(sender, receiver, token_address, num_tokens, tx_id):
    tx = web3().eth.getTransaction(tx_id)
    if not tx:
        raise AssertionError('Transaction is not found, or is not yet verified.')

    if tx['from'] != sender or tx['to'] != token_address:
        raise AssertionError(
            f'Sender and receiver in the transaction {tx_id} '
            f'do not match the expected consumer and provider addresses.'
        )

    while tx['blockNumber'] is None:
        time.sleep(0.1)
        tx = web3().eth.getTransaction(tx_id)

    block = tx['blockNumber']
    assert block, f'invalid block number {block}'
    dt_contract = DataTokenContract(token_address)

    transfer_event = dt_contract.get_transfer_event(block, sender, receiver)
    if not transfer_event:
        raise AssertionError(f'Invalid transaction {tx_id}.')

    if transfer_event.args['from'] != sender or transfer_event.args['to'] != receiver:
        raise AssertionError(f'The transfer event from/to do not match the expected values.')

    balance = dt_contract.contract.functions.balanceOf(receiver).call(block_identifier=block-1)
    try:
        new_balance = dt_contract.contract.functions.balanceOf(receiver).call(block_identifier=block)
        if (new_balance - balance) != transfer_event.args.value:
            raise AssertionError(f'Balance increment {(new_balance - balance)} does not match the Transfer '
                                 f'event value {transfer_event.args.value}.')

    except BlockNumberOutofRange as e:
        print(f'Block number {block} out of range error: {e}.')
    except AssertionError:
        raise

    if transfer_event.args.value < num_tokens:
        raise AssertionError(
            f'The transfered number of data tokens {transfer_event.args.value} does not match '
            f'the expected amount of {num_tokens} tokens')

    return True


def validate_transfer_not_used_for_other_service(did, service_id, transfer_tx_id, consumer_address, token_address):
    logger.debug(
        f'validate_transfer_not_used_for_other_service: '
        f'did={did}, service_id={service_id}, transfer_tx_id={transfer_tx_id}, '
        f'consumer_address={consumer_address}, token_address={token_address}'
    )
    return


def record_consume_request(did, service_id, transfer_tx_id, consumer_address, token_address, amount):
    logger.debug(
        f'record_consume_request: '
        f'did={did}, service_id={service_id}, transfer_tx_id={transfer_tx_id}, '
        f'consumer_address={consumer_address}, token_address={token_address}, '
        f'amount={amount}'
    )
    return


def process_consume_request(data, method, additional_params=None, require_signature=True):
    required_attributes = [
        'documentId',
        'serviceId',
        'serviceType',
        'dataToken',
        'consumerAddress'
    ]
    if additional_params:
        required_attributes += additional_params

    if require_signature:
        required_attributes.append('signature')

    msg, status = check_required_attributes(
        required_attributes, data, method)
    if msg:
        raise AssertionError(msg)

    did = data.get('documentId')
    token_address = data.get('dataToken')
    consumer_address = data.get('consumerAddress')
    service_id = data.get('serviceId')
    service_type = data.get('serviceType')

    # grab asset for did from the metadatastore associated with the Data Token address
    asset = get_asset_for_data_token(token_address, did)
    service = ServiceAgreement.from_ddo(service_type, asset)
    if service.type != service_type:
        raise AssertionError(
            f'Requested service with id {service_id} has type {service.type} which '
            f'does not match the requested service type {service_type}.'
        )

    if require_signature:
        # Raises ValueError when signature is invalid
        signature = data.get('signature')
        verify_signature(consumer_address, signature, did)

    return asset, service, did, consumer_address, token_address


def process_compute_request(data):
    required_attributes = [
        'signature',
        'consumerAddress'
    ]
    msg, status = check_required_attributes(required_attributes, data, 'compute')
    if msg:
        raise BadRequestError(msg)

    provider_acc = get_provider_account()
    did = data.get('documentId')
    owner = data.get('consumerAddress')
    job_id = data.get('jobId')
    body = dict()
    body['providerAddress'] = provider_acc.address
    if owner is not None:
        body['owner'] = owner
    if job_id is not None:
        body['jobId'] = job_id
    if did is not None:
        body['documentId'] = did

    # Consumer signature
    signature = data.get('signature')
    original_msg = f'{body.get("owner", "")}{body.get("jobId", "")}{body.get("documentId", "")}'
    verify_signature(owner, signature, original_msg)

    msg_to_sign = f'{provider_acc.address}{body.get("jobId", "")}{body.get("documentId", "")}'
    msg_hash = add_ethereum_prefix_and_hash_msg(msg_to_sign)
    body['providerSignature'] = Web3Helper.sign_hash(msg_hash, provider_acc)
    return body


def build_stage_algorithm_dict(consumer_address, algorithm_did, algorithm_token_address, algorithm_tx_id,
                               algorithm_meta, provider_account, receiver_address=None):
    if algorithm_did is not None:
        assert algorithm_token_address and algorithm_tx_id, \
            'algorithm_did requires both algorithm_token_address and algorithm_tx_id.'
        # use the DID
        if receiver_address is None:
            receiver_address = provider_account.address

        algo_asset = get_asset_for_data_token(algorithm_token_address, algorithm_did)
        service = ServiceAgreement.from_ddo(ServiceTypes.ASSET_ACCESS, algo_asset)
        validate_token_transfer(
            consumer_address,
            receiver_address,
            algorithm_token_address,
            int(service.get_cost()),
            algorithm_tx_id
        )
        validate_transfer_not_used_for_other_service(algorithm_did, service.index, algorithm_tx_id, consumer_address, algorithm_token_address)

        algo_id = algorithm_did
        raw_code = ''
        algo_url = get_asset_url_at_index(0, algo_asset, provider_account)
        container = algo_asset.metadata['main']['algorithm']['container']
    else:
        algo_id = ''
        algo_url = algorithm_meta.get('url')
        raw_code = algorithm_meta.get('rawcode')
        container = algorithm_meta.get('container')

    return dict({
        'id': algo_id,
        'url': algo_url,
        'rawcode': raw_code,
        'container': container
    })


def build_stage_output_dict(output_def, asset, owner, provider_account):
    config = get_config()
    service_endpoint = asset.get_service(ServiceTypes.CLOUD_COMPUTE).service_endpoint
    if BaseURLs.ASSETS_URL in service_endpoint:
        service_endpoint = service_endpoint.split(BaseURLs.ASSETS_URL)[0]

    return dict({
        'nodeUri': output_def.get('nodeUri', config.network_url),
        'brizoUri': output_def.get('brizoUri', service_endpoint),
        'brizoAddress': output_def.get('brizoAddress', provider_account.address),
        'metadata': output_def.get('metadata', dict({
            'main': {
                'name': 'Compute job output'
            },
            'additionalInformation': {
                'description': 'Output from running the compute job.'
            }
        })),
        'metadataUri': output_def.get('metadataUri', config.aquarius_url),
        'owner': output_def.get('owner', owner),
        'publishOutput': output_def.get('publishOutput', 1),
        'publishAlgorithmLog': output_def.get('publishAlgorithmLog', 1),
        'whitelist': output_def.get('whitelist', [])
    })


def build_stage_dict(input_dict, algorithm_dict, output_dict):
    return dict({
        'index': 0,
        'input': [input_dict],
        'compute': {
            'Instances': 1,
            'namespace': "ocean-compute",
            'maxtime': 3600
        },
        'algorithm': algorithm_dict,
        'output': output_dict
    })


def validate_algorithm_dict(algorithm_dict, algorithm_did):
    if algorithm_did and not algorithm_dict['url']:
        return f'cannot get url for the algorithmDid {algorithm_did}', 400

    if not algorithm_dict['url'] and not algorithm_dict['rawcode']:
        return f'`algorithmMeta` must define one of `url` or `rawcode`, but both seem missing.', 400

    container = algorithm_dict['container']
    # Validate `container` data
    if not (container.get('entrypoint') and container.get('image') and container.get('tag')):
        return f'algorithm `container` must specify values for all of entrypoint, image and tag.', 400

    return None, None