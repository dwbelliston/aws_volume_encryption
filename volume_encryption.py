#! /usr/local/bin/python3

#Overview:
#    Take unencrypted root volume and encrypt it for EC2.
#Params:
#    ID for EC2 instance
#    Customer Master Key (CMK) (optional)
#Conditions:
#    Return if volume already encrypted
#    Use named profiles from credentials file

import sys
import boto3
import argparse

def main(argv):
    parser = argparse.ArgumentParser(description='Encrypts EC2 root volume.')
    parser.add_argument('-i', '--instance', help='Instance to encrypt volume on.',required=True)
    parser.add_argument('-key','--customer_master_key',help='Customer master key', required=False)
    parser.add_argument('-p','--profile',help='Profile to use', required=False)
    args = parser.parse_args()

    # Set up AWS Client + Resources + Waiters
    if args.profile:
        # Create custom session
        print('Using Profile {}'.format(args.profile))
        session = boto3.session.Session(profile_name=args.profile)
    else:
        # Use default session
        session = boto3.session.Session()

    client = session.client('ec2')
    ec2 = session.resource('ec2')
    waiter_snapshot_complete = client.get_waiter('snapshot_completed')
    waiter_volume_available = client.get_waiter('volume_available')

    # Get Instance
    instance_id = args.instance
    print('---Instance {}'.format(instance_id))
    instance = ec2.Instance(instance_id)

    if not instance:
        print('No instance found with ID {}'.format(instance_id))
        sys.exit()

    # Get CMK
    customer_master_key = args.customer_master_key

    ###### Steps:
    # 1.Shut down if running
    if instance.state['Code'] is 16:
        instance.stop()

    # 2.Take snapshot
    for v in instance.volumes.all():
        volume_id = v.id
        if v.encrypted:
            print('**Volume already is encrypted')
            sys.exit()

    print('---Create snapshot of volume {}'.format(volume_id))
    snapshot = ec2.create_snapshot(
        VolumeId=volume_id,
        Description='Snapshot of {}'.format(volume_id),
    )

    waiter_snapshot_complete.wait(
        SnapshotIds=[
            snapshot.id,
        ]
    )

    # 3.Create new encrypted volume(same size)
    print('---Create Encrypted Snapshot Copy')
    if customer_master_key:
        # Use custom key
        snapshot_encrypted = snapshot.copy(
            SourceRegion=session.region_name,
            Description='Encrypted Copied Snapshot of {}'.format(snapshot.id),
            KmsKeyId=customer_master_key,
            Encrypted=True,
        )
    else:
        # Use default key
        snapshot_encrypted = snapshot.copy(
            SourceRegion=session.region_name,
            Description='Encrypted Copied Snapshot of {}'.format(snapshot.id),
            Encrypted=True,
        )

    waiter_snapshot_complete.wait(
        SnapshotIds=[
            snapshot_encrypted['SnapshotId'],
        ],
    )

    print('---Create Encrypted Volume from snapshot')
    volume_encrypted = ec2.create_volume(
        SnapshotId=snapshot_encrypted['SnapshotId'],
        AvailabilityZone=instance.placement['AvailabilityZone']
    )

    # 4.Detach current root volume
    print('---Deatch Volume {}'.format(volume_id))
    instance.detach_volume(
        VolumeId=volume_id,
        Device=instance.root_device_name,
    )

    # 5.Attach new volume
    print('---Attach Volume {}'.format(volume_encrypted.id))
    waiter_volume_available.wait(
        VolumeIds=[
            volume_encrypted.id,
        ],
    )

    instance.attach_volume(
        VolumeId=volume_encrypted.id,
        Device=instance.root_device_name
    )

    # 6.Restart instance
    print('---Restart Instance')
    if instance.state['Code'] is 80 or instance.state['Code'] is 64:
        instance.start()

    # Clean up
    snapshot.delete()

    print('Fin')

if __name__ == "__main__":
   main(sys.argv[1:])
