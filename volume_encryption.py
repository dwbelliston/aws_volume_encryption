#! /Users/dwbelliston/.virtualenvs/aws_ebs/bin/python

"""
Overview:
    Take unencrypted root volume and encrypt it for EC2.
Params:
    ID for EC2 instance
    Customer Master Key (CMK) (optional)
    Profile to use
Conditions:
    Return if volume already encrypted
    Use named profiles from credentials file
"""

import sys
import boto3
import botocore
import argparse


def main(argv):
    parser = argparse.ArgumentParser(description='Encrypts EC2 root volume.')
    parser.add_argument('-i', '--instance',
                        help='Instance to encrypt volume on.', required=True)
    parser.add_argument('-key', '--customer_master_key',
                        help='Customer master key', required=False)
    parser.add_argument('-p', '--profile',
                        help='Profile to use', required=False)
    args = parser.parse_args()

    """ Set up AWS Session + Client + Resources + Waiters """
    if args.profile:
        # Create custom session
        print('Using profile {}'.format(args.profile))
        session = boto3.session.Session(profile_name=args.profile)
    else:
        # Use default session
        session = boto3.session.Session()

    # Get CMK
    customer_master_key = args.customer_master_key

    client = session.client('ec2')
    ec2 = session.resource('ec2')

    waiter_instance_exists = client.get_waiter('instance_exists')
    waiter_instance_stopped = client.get_waiter('instance_stopped')
    waiter_instance_running = client.get_waiter('instance_running')
    waiter_snapshot_complete = client.get_waiter('snapshot_completed')
    waiter_volume_available = client.get_waiter('volume_available')

    """ Check instance exists """
    instance_id = args.instance
    print('---Checking instance ({})'.format(instance_id))
    instance = ec2.Instance(instance_id)

    try:
        waiter_instance_exists.wait(
            InstanceIds=[
                instance_id,
            ]
        )
    except botocore.exceptions.WaiterError as e:
        sys.exit('ERROR: {}'.format(e))

    """ Get volume and exit if already encrypted """
    volumes = [v for v in instance.volumes.all()]
    if volumes:
        original_root_volume = volumes[0]
        volume_encrypted = original_root_volume.encrypted
        if volume_encrypted:
            sys.exit(
                '**Volume ({}) is already encrypted'
                .format(original_root_volume.id))

    """ Step 1: Prepare instance """
    print('---Preparing instance')
    # Save original mappings to persist to new volume
    original_mappings = {}
    original_mappings['DeleteOnTermination'] = instance.block_device_mappings[0]['Ebs']['DeleteOnTermination']

    # Exit if instance is pending, shutting-down, or terminated
    instance_exit_states = [0, 32, 48]
    if instance.state['Code'] in instance_exit_states:
        sys.exit(
            'ERROR: Instance is {} please make sure this instance is active.'
            .format(instance.state['Name'])
        )

    # Validate successful shutdown if it is running or stopping
    if instance.state['Code'] is 16:
        instance.stop()

    # Set the max_attempts for this waiter (default 40)
    waiter_instance_stopped.config.max_attempts = 40

    try:
        waiter_instance_stopped.wait(
            InstanceIds=[
                instance_id,
            ]
        )
    except botocore.exceptions.WaiterError as e:
        sys.exit('ERROR: {}'.format(e))

    """ Step 2: Take snapshot of volume """
    print('---Create snapshot of volume ({})'.format(original_root_volume.id))
    snapshot = ec2.create_snapshot(
        VolumeId=original_root_volume.id,
        Description='Snapshot of volume ({})'.format(original_root_volume.id),
    )

    try:
        waiter_snapshot_complete.wait(
            SnapshotIds=[
                snapshot.id,
            ]
        )
    except botocore.exceptions.WaiterError as e:
        snapshot.delete()
        sys.exit('ERROR: {}'.format(e))

    """ Step 3: Create encrypted volume """
    print('---Create encrypted copy of snapshot')
    if customer_master_key:
        # Use custom key
        snapshot_encrypted_dict = snapshot.copy(
            SourceRegion=session.region_name,
            Description='Encrypted copy of snapshot #{}'
                        .format(snapshot.id),
            KmsKeyId=customer_master_key,
            Encrypted=True,
        )
    else:
        # Use default key
        snapshot_encrypted_dict = snapshot.copy(
            SourceRegion=session.region_name,
            Description='Encrypted copy of snapshot ({})'
                        .format(snapshot.id),
            Encrypted=True,
        )

    snapshot_encrypted = ec2.Snapshot(snapshot_encrypted_dict['SnapshotId'])

    try:
        waiter_snapshot_complete.wait(
            SnapshotIds=[
                snapshot_encrypted.id,
            ],
        )
    except botocore.exceptions.WaiterError as e:
        snapshot.delete()
        snapshot_encrypted.delete()
        sys.exit('ERROR: {}'.format(e))

    print('---Create encrypted volume from snapshot')
    volume_encrypted = ec2.create_volume(
        SnapshotId=snapshot_encrypted.id,
        AvailabilityZone=instance.placement['AvailabilityZone']
    )

    """ Step 4: Detach current root volume """
    print('---Deatch volume {}'.format(original_root_volume.id))
    instance.detach_volume(
        VolumeId=original_root_volume.id,
        Device=instance.root_device_name,
    )

    """ Step 5: Attach current root volume """
    print('---Attach volume {}'.format(volume_encrypted.id))
    try:
        waiter_volume_available.wait(
            VolumeIds=[
                volume_encrypted.id,
            ],
        )
    except botocore.exceptions.WaiterError as e:
        snapshot.delete()
        snapshot_encrypted.delete()
        volume_encrypted.delete()
        sys.exit('ERROR: {}'.format(e))

    instance.attach_volume(
        VolumeId=volume_encrypted.id,
        Device=instance.root_device_name
    )

    """ Step 6: Restart instance """
    # Modify instance attributes
    instance.modify_attribute(
        BlockDeviceMappings=[
            {
                'DeviceName': instance.root_device_name,
                'Ebs': {
                    'DeleteOnTermination':
                    original_mappings['DeleteOnTermination'],
                },
            },
        ],
    )
    print('---Restart instance')
    instance.start()

    try:
        waiter_instance_running.wait(
            InstanceIds=[
                instance_id,
            ]
        )
    except botocore.exceptions.WaiterError as e:
        sys.exit('ERROR: {}'.format(e))

    """ Step 7: Clean up """
    print('---Clean up resources')
    # Delete snapshots and original volume
    snapshot.delete()
    snapshot_encrypted.delete()
    original_root_volume.delete()

    print('Encryption finished')

if __name__ == "__main__":
    main(sys.argv[1:])
