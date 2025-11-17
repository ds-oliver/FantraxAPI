# Secret ID Authentication

## What is a Fantrax Secret ID?

Your Fantrax Secret ID is a **read-only credential** that allows this app to verify your identity without storing your password. Even if someone obtains your Secret ID, they can only **view** your league information in read-only contexts - they **cannot** make roster changes, drops, trades, or waiver claims.

## Why We Use Secret IDs

1. **No Password Storage** - We never store or see your Fantrax password
2. **Limited Risk** - Secret ID is read-only in most contexts
3. **User Controlled** - You can regenerate it anytime on Fantrax.com
4. **Revocable** - Delete it from our app at any time
5. **Proof of Ownership** - Only the account owner can access the Secret ID

## How to Get Your Secret ID

### Step 1: Log into Fantrax
Visit [Fantrax.com](https://www.fantrax.com) and log in to your account.

### Step 2: Go to Your Profile
Navigate to your profile page: **[https://www.fantrax.com/user/profile](https://www.fantrax.com/user/profile)**

### Step 3: Find Your Secret ID
On your profile page, look for the section labeled **"Secret ID"**. It will be a long alphanumeric string (e.g., `abc123def456...`).

### Step 4: Copy Your Secret ID
Click the copy button or manually select and copy the entire Secret ID.

### Step 5: Enter it in the App
Paste your Secret ID into the authentication form in this app.

## Security Features

- **Encrypted Storage** - Your Secret ID is encrypted using industry-standard AES encryption
- **Hashed Verification** - We use one-way hashing to verify without storing plaintext
- **Secure Key Management** - Encryption keys are stored separately with restricted permissions
- **No Network Transmission** - Secret ID stays on your machine (only cookies are used for Fantrax API)

## Managing Your Secret ID

### View Status
You can see if your Secret ID is currently saved in the app settings.

### Revoke Access
Click the **"Revoke Secret ID"** button to:
- Delete your Secret ID from our database
- Clear your authentication (you'll need to re-enter it to use the app)
- Not affect your Fantrax account (Secret ID still works on Fantrax)

### Regenerate on Fantrax
If you believe your Secret ID was compromised:
1. Go to [https://www.fantrax.com/user/profile](https://www.fantrax.com/user/profile)
2. Click "Regenerate Secret ID"
3. Copy the new Secret ID
4. Enter it in this app (it will replace the old one)

## Frequently Asked Questions

**Q: Can someone use my Secret ID to change my roster?**  
A: No. In this app, the Secret ID is only used for authentication. Roster changes require your actual session cookies from a full Fantrax login.

**Q: What if I lose my Secret ID?**  
A: You can always get it from your Fantrax profile page.

**Q: Can I use the same Secret ID on multiple devices?**  
A: Yes, your Secret ID is the same across all devices until you regenerate it.

**Q: Does this app send my Secret ID to Fantrax servers?**  
A: No. We only use it locally to verify your identity. Your actual Fantrax API calls use session cookies, not the Secret ID.

**Q: What if someone gains access to my computer?**  
A: They could potentially access the encrypted Secret ID file. However:
- It's encrypted with a key only on your machine
- They'd also need access to your cookie files to actually make changes
- You can revoke access and regenerate your Secret ID on Fantrax

## Support

If you have questions or concerns about Secret ID authentication, please review the [Fantrax API documentation](https://www.fantrax.com/api) or contact support.

