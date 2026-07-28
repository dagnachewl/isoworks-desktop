# IsoWorks LIMS — Administrator Password Reset Guide

This guide describes the password management system in IsoWorks LIMS and outlines the procedures for Super Administrators to verify identity and reset employee passwords securely.

---

## 1. Overview of Password Management

IsoWorks Web enforces secure password storage using the **bcrypt** hashing algorithm. 
* **Initial/Default Passwords**: When a new employee account is seeded or created, their default password is set to their **lowercase System Login Name** (e.g., username `mac` has default password `mac`).
* **Self-Service Password Change**: Users who know their current password can change it directly from the Sign In dialog by clicking **Change Password?**.
* **Password Reset (Forgot Password)**: Users who have forgotten their password must request a reset from a Super Administrator. This manual intervention prevents unauthorized lockout and security exploits.

---

## 2. The User Password Reset Request Flow

When a user cannot sign in:
1. They click **Forgot Password?** on the login screen.
2. They enter their **System Login Name** and click **Request Reset**.
3. **Backend Actions**:
   * The backend validates that the username exists and is active.
   * The backend queries the database for all active employees with the `Super_Admin` role.
   * If SMTP settings are configured in `.env`, the system sends a notification email to those administrators.
   * A full notification template is written to the server's backend log (`/tmp/isoworks-backend.log` or console output).
4. **Frontend Feedback**: The user is redirected back to the login screen with a confirmation banner listing the active administrators they should contact to approve the request:
   > *Reset request submitted! Please ask a Super Admin (mac, mckayj) to approve and perform the reset.*

---

## 3. Resetting a Specific User's Password (CLI)

To perform the reset, a Super Admin must execute the secure administration script on the server host.

### Prerequisites
1. Access to the server terminal.
2. The `isoworks` Conda environment must be installed.
3. A valid Super Admin login name and password.

### Step-by-Step Execution

1. Navigate to the IsoWorks directory on the server:
   ```bash
   cd /Users/mac/Downloads/IsoWorks_PyQt_Pg
   ```

2. Run the secure reset script using Conda:
   ```bash
   conda run -n isoworks python backend/scripts/reset_user_password.py
   ```

3. **Provide Admin Credentials**:
   * The script prompts for your **Admin Username** and **Admin Password**.
   * It checks these credentials against the database and verifies you possess the `Super_Admin` role. If unauthorized, it exits immediately.

4. **Enter Target User**:
   * Enter the **System Login Name** of the employee whose password needs to be reset.
   * The script checks that the target employee exists.
   * If the employee is marked as obsolete/inactive, it displays a warning and asks you to confirm.

5. **Success Confirmation**:
   * The script updates the user's password hash in the database to a fresh bcrypt hash of their **lowercase login name**.
   * Inform the user that their password has been reset. Tell them to log in with their lowercase username and immediately use **Change Password?** to set a secure custom password.

### CLI Example Output
```
=== IsoWorks Password Reset Tool ===
Admin Username: mac
Admin Password: 
Authenticated as Super Admin: Dagnachew L. BELACHEW
----------------------------------------
Enter System Login Name of the user to reset: johnd
Success: Password for 'johnd' has been reset to default (lowercase username: 'johnd').
```

---

## 4. Resetting All Passwords in Bulk (System Seeding)

If you are performing a system migration, setting up a new server environment, or initializing password hashes for all employees for the first time, you can run the seeding tool:

```bash
conda run -n isoworks python backend/scripts/seed_passwords.py
```

> [!CAUTION]
> **Data Overwrite Warning**: Running this script will overwrite **all** custom passwords previously set by active employees and reset them to their default lowercase login names. Only run this during migration phases or system setups.

---

## 5. Troubleshooting & Configuration

### A. SMTP / Email Dispatch Failures
If administrators are not receiving notification emails:
1. Verify SMTP variables are set up in the `.env` file at the root of the project:
   ```env
   SMTP_HOST=smtp.yourserver.com
   SMTP_PORT=587
   SMTP_USER=your_smtp_user
   SMTP_PASSWORD=your_smtp_password
   SMTP_FROM=noreply@yourdomain.com
   ```
2. If SMTP is not set up, check the server's background log to find the request details:
   ```bash
   cat /tmp/isoworks-backend.log | grep "DEV PASSWORD RESET REQUEST" -A 10
   ```

### B. "UndefinedTable" or Connection Issues
If the CLI scripts fail with a database error:
1. Ensure the PostgreSQL server is running:
   ```bash
   pg_ctl -D /usr/local/var/postgres status
   ```
2. Verify the `DB_URL` configuration inside the `.env` file matches your active PostgreSQL connection details.
